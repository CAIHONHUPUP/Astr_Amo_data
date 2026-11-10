# -*- coding: utf-8 -*-
"""测试入口：先把临时目录指到工作区（本机沙箱不让写系统 temp），再发现全部用例。"""
import os
import sys
import tempfile
import unittest
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(BASE, "_test_tmp")
os.makedirs(TMP, exist_ok=True)
tempfile.tempdir = TMP
os.environ["AMO_DATA_TEST_TMP"] = TMP
sys.path.insert(0, BASE)


def _mkdtemp(suffix="", prefix="tmp", dir=None):
    """mkdtemp 建出来的 0700 目录在本机沙箱里写不进去，换成普通 os.makedirs。"""
    base = dir or tempfile.gettempdir()
    path = os.path.join(base, str(prefix) + uuid.uuid4().hex[:10] + str(suffix))
    os.makedirs(path, exist_ok=False)
    return path


tempfile.mkdtemp = _mkdtemp

if __name__ == "__main__":
    TESTS = os.path.join(BASE, "tests")
    suite = unittest.defaultTestLoader.discover(TESTS, pattern="test_*.py", top_level_dir=TESTS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
