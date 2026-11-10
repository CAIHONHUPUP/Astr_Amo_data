# -*- coding: utf-8 -*-
"""检索增强（相关性闸 / 去重 / 遗忘 / 语义）的单测。

对应 store.py 的 patch：
- `min_matched_terms`：关键词证据不足且语义也不像时返回空，不再"硬推一条"；
- `auto_dedupe`：完全重复 + 近重复都合并；
- `created_at` 参数：迁移过来的旧记忆按真实时间算新鲜度；
- 语义闸：只看 top 与第二名的余弦差，挡住"无关问题也能命中"。
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class RecallGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))
        self.store.remember(BOT, UID, "2026-11-06 晚上，主人吃了牛杂和小烧烤，还拍了照片发给我看。", importance=0.7)
        self.store.remember(BOT, UID, "主人今晚十点多跟我说，以后飞书是主战场，有事都去飞书找他。", importance=0.6)
        self.store.remember(BOT, UID, "主人养了一只叫团子的猫，橘色的。", importance=0.5)

    def tearDown(self):
        self.store.close()

    def test_question_shell_still_hits(self):
        """整句问句（amo 实际传的格式）要能命中，这是 2.2.8 的老毛病。"""
        rows = self.store.recall(BOT, UID, "你还记得我周五晚上吃了什么吗")
        self.assertTrue(rows, "整句问句应该命中")
        self.assertIn("牛杂", rows[0]["content"])

    def test_two_char_query_hits(self):
        self.assertIn("牛杂", self.store.recall(BOT, UID, "牛杂")[0]["content"])
        self.assertIn("团子", self.store.recall(BOT, UID, "团子")[0]["content"])

    def test_no_false_positive(self):
        """库里没有相关内容时必须返回空，而不是给一条高分的无关记忆。"""
        self.assertEqual(self.store.recall(BOT, UID, "上次说的小挂件呢"), [])
        self.assertEqual(self.store.recall(BOT, UID, "外面下雨了吗"), [])

    def test_generic_time_word_is_not_evidence(self):
        """「今天」这种泛指不能单独当作命中证据。"""
        self.assertEqual(self.store.recall(BOT, UID, "你今天心情怎么样"), [])

    def test_min_matched_terms_can_be_disabled(self):
        store = DataStore(
            os.path.join(self.tmp, "off.db"), settings={"min_matched_terms": 0}
        )
        try:
            store.remember(BOT, UID, "主人养了一只叫团子的猫。", importance=0.5)
            self.assertTrue(store.recall(BOT, UID, "上次说的小挂件呢"),
                            "关掉闸之后恢复成「总会给一条」的旧行为")
        finally:
            store.close()

    def test_scope_isolation(self):
        self.store.remember(BOT, UID, "群里有人说周末去爬山", scope="group", group_id="g1")
        rows = self.store.recall(BOT, UID, "爬山", scope="private")
        self.assertFalse([r for r in rows if "爬山" in r["content"]])


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))

    def tearDown(self):
        self.store.close()

    def _count(self):
        return int(self.store.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])

    def test_exact_duplicate(self):
        a = self.store.remember(BOT, UID, "主人养了一只叫团子的猫，橘色的。", importance=0.5)
        b = self.store.remember(BOT, UID, "主人养了一只叫团子的猫，橘色的。", importance=0.8)
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(self._count(), 1)
        row = self.store.conn.execute("SELECT importance FROM memories WHERE id=?", (a["memory_id"],)).fetchone()
        self.assertAlmostEqual(float(row["importance"]), 0.8, places=3)

    def test_near_duplicate_punctuation(self):
        a = self.store.remember(BOT, UID, "2026-11-06 晚上，主人吃了牛杂和小烧烤，还拍了照片发给我看。")
        b = self.store.remember(BOT, UID, "2026-11-06 晚上主人吃了牛杂和小烧烤还拍了照片发给我看！")
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["memory_id"], b["memory_id"])
        self.assertEqual(self._count(), 1)
        row = self.store.conn.execute("SELECT merged_count FROM memories WHERE id=?", (a["memory_id"],)).fetchone()
        self.assertGreaterEqual(int(row["merged_count"] or 1), 2)

    def test_dedupe_can_be_disabled(self):
        store = DataStore(os.path.join(self.tmp, "nod.db"), settings={"auto_dedupe": False})
        try:
            store.remember(BOT, UID, "主人喜欢喝美式咖啡。")
            store.remember(BOT, UID, "主人喜欢喝美式咖啡。")
            n = int(store.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
            self.assertEqual(n, 2)
        finally:
            store.close()


class ForgettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))

    def tearDown(self):
        self.store.close()

    def test_old_memory_sinks(self):
        old = int(time.time()) - 60 * 86400
        self.store.remember(BOT, UID, "六十天前的旧事一件", importance=0.5, created_at=old, last_hit_at=old)
        self.store.remember(BOT, UID, "刚刚说的新鲜事一件", importance=0.5)
        rows = self.store.recall(BOT, UID, "")
        self.assertIn("新鲜", rows[0]["content"])

    def test_high_importance_decays_slower(self):
        old = int(time.time()) - 60 * 86400
        self.store.remember(BOT, UID, "很重要的事：生日是十二月十号", importance=0.95,
                            created_at=old, last_hit_at=old)
        self.store.remember(BOT, UID, "不重要的小事一桩", importance=0.2)
        rows = self.store.recall(BOT, UID, "")
        self.assertIn("生日", rows[0]["content"])

    def test_migrated_created_at_kept(self):
        old = int(time.time()) - 100 * 86400
        r = self.store.remember(BOT, UID, "迁移过来的老记忆", created_at=old)
        self.assertLessEqual(r["created_at"], old + 5)


class FakeVectorTests(unittest.TestCase):
    """用假向量验证语义闸，不依赖本机 Ollama。"""

    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(
            os.path.join(self.tmp, "vec.db"),
            settings={"embedding_enabled": True, "embedding_timeout": 0.1,
                      "vec_min_cos": 0.45, "vec_min_margin": 0.08},
        )
        self.vectors = {
            "猫": [1.0, 0.0, 0.0],
            "牛杂": [0.0, 1.0, 0.0],
            "礼物": [0.0, 0.5, 0.5],
        }
        self.store._embed = lambda text: self.vectors.get(str(text), [0.0, 0.0, 0.0])  # noqa: E731

    def tearDown(self):
        self.store.close()

    def _seed(self):
        for key, text in (("猫", "主人养了一只叫团子的猫。"),
                          ("牛杂", "主人吃了牛杂和小烧烤。"),
                          ("礼物", "他送了我一个小挂件。")):
            mid = self.store.remember(BOT, UID, text)["memory_id"]
            self.store._store_vec(mid, self.vectors[key])

    def test_semantic_hit(self):
        self._seed()
        rows = self.store.recall(BOT, UID, "猫")
        self.assertIn("团子", rows[0]["content"])

    def test_semantic_gate_rejects_flat_distribution(self):
        self._seed()
        # 三个向量都给出 0 相似 → 没有语义证据，关键词也不中 → 空
        self.assertEqual(self.store.recall(BOT, UID, "完全无关的一句话"), [])

    def test_embedding_off_by_default(self):
        store = DataStore(os.path.join(self.tmp, "off.db"))
        try:
            self.assertEqual(store.embedding_stats()["vectors"], 0)
            self.assertEqual(store.backfill_embeddings(), 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
