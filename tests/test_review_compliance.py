# -*- coding: utf-8 -*-
"""上架审核的两条硬规矩，用源码守卫盯着，别再回退。

1. logger 只能从 astrbot.api 导入，严禁回退内置 logging；
2. async 处理函数里不许同步调 store.recall（开了语义召回时会走 HTTP，会堵事件循环）。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class LoggingRuleTests(unittest.TestCase):
    def test_store_does_not_fallback_to_builtin_logging(self):
        # store.py 拆成 store_*.py 之后，守卫要扫整组模块，而不是只看入口
        names = sorted(
            name for name in os.listdir(ROOT)
            if name.startswith("store") and name.endswith(".py")
        )
        self.assertIn("store.py", names)
        for name in names:
            body = read(name)
            self.assertNotIn("import logging", body, name)
            self.assertNotIn("logging.getLogger", body, name)
        joined = "\n".join(read(name) for name in names)
        self.assertIn("from astrbot.api import logger", joined)


class AsyncBlockingTests(unittest.TestCase):
    def test_api_memories_offloads_recall(self):
        body = read("main.py")
        start = body.index("async def api_memories")
        end = body.index("async def api_memory_save", start)
        segment = body[start:end]
        self.assertIn("asyncio.to_thread", segment)
        self.assertNotIn("self.store.recall(", segment)

    def test_memory_api_recall_offloads(self):
        body = read("main.py")
        start = body.index("async def recall", body.index("class MemoryAPI"))
        segment = body[start:start + 900]
        self.assertIn("asyncio.to_thread", segment)


if __name__ == "__main__":
    unittest.main()
