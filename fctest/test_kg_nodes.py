# -*- coding: utf-8 -*-
"""
图谱数据层测试
==============

验证 MySQL 里的节点/边/别名数据**可用**，以及「文本 → 节点 → 科室」链路**走得通**。

与其它测试集的分工：
    test_data_integrity.py  —— CSV 产物完整性（不碰数据库）
    test_kg_layer2.py       —— 工具集 / 遍历投票 / 节点选择器
    test_llm_config.py      —— LLM 配置中心（不碰数据库）
    本文件                   —— 数据库里的图数据健康度 + 端到端链路

运行：
    python tests/kg/fctest/run_tests.py nodes
    python tests/kg/fctest/run_tests.py nodes -v
"""

import json
import sys
import unittest

from test_config import TESTS_DIR

if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import kgapi                                                # noqa: E402
import db as kgdb                                           # noqa: E402
from store import get_store                                 # noqa: E402
from recommend import traverse, is_pediatric_department     # noqa: E402

DB_OK, DB_DESC = kgdb.health()


# ======================================================================
# 期望值：业务底线，数据退化时必须报警
# ======================================================================
EXPECTED_MIN = {
    "节点总数": 15000,
    "边总数":   80000,
    "别名总数": 8000,
    "症状数":   5000,
    "疾病数":   7000,
    "科室数":   200,
}

# 端到端：输入 → 期望排第一的科室（来自医学常识，不是从数据反推）
TRAVERSE_CASES = [
    ("腿疼",    "骨科专科"),
    ("上不来气", "呼吸内科"),
    ("拉肚子",  "消化专科"),
    ("牙疼",    "口腔内科"),
]

# 必须能链接到节点的口语
LINK_MUST_HIT = ["腿疼", "上不来气", "拉肚子", "睡眠不好", "心口疼"]

# 必须被过滤的真否定
LINK_MUST_MISS = ["腰没有疼", "肚子不疼"]


def _extra(row):
    """把 extra 字段统一成 dict"""
    ex = (row or {}).get("extra")
    if isinstance(ex, str):
        try:
            return json.loads(ex) or {}
        except Exception:
            return {}
    return ex or {}


def link(text):
    """文本 → 节点键列表（走 store 的别名/名称匹配）"""
    r = get_store().search_nodes(text)
    return [n["node_key"] for n in r["nodes"]
            if n.get("match") in ("alias", "name_exact")]


@unittest.skipUnless(DB_OK, "数据库不可用: %s" % DB_DESC)
class DBTestCase(unittest.TestCase):
    """数据库测试基类"""

    @classmethod
    def setUpClass(cls):
        if kgapi.node_count() < 1000:
            raise unittest.SkipTest(
                "图谱数据未导入，请先运行:\n"
                "  python tests/kg/builders/import_kg_nodes.py")


# ======================================================================
class TestNodeData(DBTestCase):
    """1. 节点数据完整性"""

    def test_scale_meets_minimum(self):
        """各类型节点数不低于业务底线"""
        actual = {
            "节点总数": kgapi.node_count(),
            "边总数":   kgapi.edge_count(),
            "别名总数": kgapi.alias_count(),
            "症状数":   kgapi.node_count("symptom"),
            "疾病数":   kgapi.node_count("disease"),
            "科室数":   kgapi.node_count("department"),
        }
        bad = {k: (v, EXPECTED_MIN[k]) for k, v in actual.items() if v < EXPECTED_MIN[k]}
        self.assertFalse(bad, "以下指标低于底线（实际, 期望）: %s" % bad)

    def test_node_key_wellformed(self):
        """⭐ node_key 必须是「类型:名称」且类型与 node_type 一致"""
        bad = []
        for row in kgapi.all_nodes(limit=3000):
            k, t = row["node_key"], row["node_type"]
            if ":" not in k:
                bad.append(("缺冒号", k))
            elif k.split(":", 1)[0] != t:
                bad.append(("类型不一致", k, t))
        self.assertFalse(bad, "node_key 格式异常 %d 条，示例: %s" % (len(bad), bad[:5]))

    def test_no_empty_names(self):
        """不存在空名字节点"""
        r = kgdb.query_one("SELECT COUNT(*) AS c FROM kg_node WHERE name='' OR name IS NULL")
        self.assertEqual(r["c"], 0, "有 %d 个空名字节点" % r["c"])

    def test_no_duplicate_node_key(self):
        """node_key 必须唯一（唯一约束的兜底校验）"""
        r = kgdb.query_one(
            "SELECT COUNT(*) AS c FROM (SELECT node_key FROM kg_node "
            "GROUP BY node_key HAVING COUNT(*)>1) t")
        self.assertEqual(r["c"], 0, "有 %d 个重复的 node_key" % r["c"])

    def test_edge_endpoints_exist(self):
        """⭐ 所有边两端必须指向存在的节点（引用完整性）"""
        r1 = kgdb.query_one(
            "SELECT COUNT(*) AS c FROM kg_edge e "
            "LEFT JOIN kg_node n ON n.node_key=e.src_key WHERE n.node_key IS NULL")
        r2 = kgdb.query_one(
            "SELECT COUNT(*) AS c FROM kg_edge e "
            "LEFT JOIN kg_node n ON n.node_key=e.dst_key WHERE n.node_key IS NULL")
        self.assertEqual(r1["c"], 0, "有 %d 条边的 src_key 找不到节点" % r1["c"])
        self.assertEqual(r2["c"], 0, "有 %d 条边的 dst_key 找不到节点" % r2["c"])

    def test_alias_targets_exist(self):
        """⭐ 所有别名必须指向存在的节点"""
        r = kgdb.query_one(
            "SELECT COUNT(*) AS c FROM kg_alias a "
            "LEFT JOIN kg_node n ON n.node_key=a.node_key WHERE n.node_key IS NULL")
        self.assertEqual(r["c"], 0, "有 %d 条别名指向不存在的节点" % r["c"])

    def test_departments_bookable(self):
        """⭐ 科室节点必须标记为可挂号"""
        not_bookable = [r["name"] for r in kgdb.query(
            "SELECT name, extra FROM kg_node WHERE node_type='department'")
            if not _extra(r).get("bookable")]
        self.assertLessEqual(len(not_bookable), 5,
                             "有 %d 个科室未标记可挂号: %s" % (len(not_bookable), not_bookable[:8]))

    def test_no_duplicate_name_type(self):
        """同名同类型不应重复"""
        rows = kgdb.query(
            "SELECT name, node_type, COUNT(*) AS c FROM kg_node "
            "GROUP BY name, node_type HAVING c>1 LIMIT 10")
        self.assertFalse(rows, "同名同类型重复 %d 组，示例: %s"
                         % (len(rows), [(r["name"], r["node_type"]) for r in rows[:5]]))


# ======================================================================
class TestEntityLinking(DBTestCase):
    """2. 文本 → 节点"""

    def test_common_colloquialisms_hit(self):
        """⭐ 常见口语必须能链接到节点"""
        miss = [t for t in LINK_MUST_HIT if not link(t)]
        self.assertFalse(miss, "以下口语无法链接到节点: %s" % miss)

    def test_true_negation_filtered(self):
        """⭐ 真否定表达必须被过滤"""
        bad = [t for t in LINK_MUST_MISS if link(t)]
        self.assertFalse(bad, "以下否定表达被错误链接: %s" % bad)

    def test_linking_no_duplicates(self):
        """同一文本的多条链接不应重复"""
        for t in LINK_MUST_HIT:
            ks = link(t)
            with self.subTest(text=t):
                self.assertEqual(len(ks), len(set(ks)), "%s 返回了重复节点" % t)

    def test_link_fanout_reasonable(self):
        """⭐ 单个口语指向的节点数不应过多（防过度泛化）"""
        counts = kgapi.alias_target_counts()
        over = [(a, c) for a, c in counts.items() if c > 5]
        self.assertFalse(over, "有 %d 个口语指向超过 5 个节点: %s" % (len(over), over[:5]))

    def test_unrelated_text_no_match(self):
        """与医学无关的文本不应命中"""
        for t in ["今天天气真好", "帮我订张机票", "1+1等于几"]:
            with self.subTest(text=t):
                self.assertEqual(link(t), [], "无关文本错误命中节点: %s" % t)


# ======================================================================
class TestGraphTraversal(DBTestCase):
    """3. 图遍历：节点 → 科室"""

    def test_traverse_reaches_departments(self):
        """⭐ 常见症状必须能遍历出科室"""
        for text, _ in TRAVERSE_CASES:
            with self.subTest(text=text):
                keys = link(text)
                if not keys:
                    self.skipTest("%s 无别名链接" % text)
                self.assertTrue(traverse(keys)["departments"],
                                "%s 遍历后未得到任何科室" % text)

    def test_traverse_top_department_correct(self):
        """⭐ 医学常识校验：推荐的第一名科室必须正确"""
        failures = []
        for text, expect in TRAVERSE_CASES:
            keys = link(text)
            if not keys:
                failures.append((text, expect, "无别名链接"))
                continue
            deps = traverse(keys)["departments"]
            got = deps[0]["科室"] if deps else None
            if got != expect:
                failures.append((text, expect, got))
        self.assertFalse(
            failures,
            "推荐结果不符合医学常识:\n%s" %
            "\n".join("  「%s」期望 %s，实际 %s" % f for f in failures))

    def test_traverse_results_are_departments(self):
        """遍历结果必须都是科室类型节点"""
        keys = link("上不来气")
        for d in traverse(keys, top_n=5)["departments"]:
            with self.subTest(dept=d["科室"]):
                self.assertTrue(
                    kgapi.get_nodes_by_name(d["科室"], "department"),
                    "%s 不是科室节点" % d["科室"])

    def test_different_symptoms_different_departments(self):
        """⭐ 区分度：不同系统的症状必须推荐不同科室"""
        got = {}
        for text in ["眼睛疼", "牙疼", "拉肚子"]:
            keys = link(text)
            if not keys:
                continue
            deps = traverse(keys, top_n=1)["departments"]
            got[text] = deps[0]["科室"] if deps else None
        distinct = len({v for v in got.values() if v})
        self.assertGreaterEqual(distinct, 2,
                                "不同症状返回了相同科室，区分度不足: %s" % got)

    def test_adult_not_referred_to_pediatric(self):
        """⭐ 成年人不被推荐儿科（实测踩过的坑）"""
        keys = link("上不来气")
        deps = traverse(keys, age=30)["departments"]
        for d in deps:
            with self.subTest(dept=d["科室"]):
                self.assertFalse(is_pediatric_department(d["科室"]),
                                 "成年人被推荐了儿科: %s" % d["科室"])


# ======================================================================
class TestAliasHygiene(DBTestCase):
    """4. 别名数据卫生"""

    def test_no_identity_aliases(self):
        """别名不应等于它指向的节点名（冗余条目）"""
        rows = kgdb.query(
            "SELECT a.alias, n.name FROM kg_alias a "
            "JOIN kg_node n ON n.node_key=a.node_key "
            "WHERE a.alias = n.name LIMIT 10")
        self.assertFalse(rows, "有 %d 条别名与其节点名相同: %s"
                         % (len(rows), [r["alias"] for r in rows[:5]]))

    def test_alias_length_reasonable(self):
        """别名长度应在 2~40 之间（单字别名太泛，会误命中）"""
        rows = kgdb.query(
            "SELECT DISTINCT alias FROM kg_alias "
            "WHERE CHAR_LENGTH(alias) < 2 OR CHAR_LENGTH(alias) > 40 LIMIT 10")
        self.assertFalse(rows, "有长度异常的别名: %s" % [r["alias"] for r in rows[:5]])

    def test_most_aliases_single_target(self):
        """⭐ 多数口语应指向唯一节点（歧义度可控）"""
        counts = kgapi.alias_target_counts()
        if not counts:
            self.skipTest("无别名数据")
        single = sum(1 for v in counts.values() if v == 1)
        ratio = single / float(len(counts))
        self.assertGreaterEqual(ratio, 0.90,
                                "单目标别名占比 %.4f (%d/%d)，低于 90%%"
                                % (ratio, single, len(counts)))


# ======================================================================
class TestCompliance(DBTestCase):
    """5. 科室分级与检查项属性"""

    def test_department_has_parent(self):
        """科室节点应尽量带 parent（一级科室名），用于分级展示"""
        no_parent = [r["name"] for r in kgdb.query(
            "SELECT name, extra FROM kg_node WHERE node_type='department'")
            if not _extra(r).get("parent")]
        self.assertLessEqual(len(no_parent), 40,
                             "有 %d 个科室缺 parent: %s" % (len(no_parent), no_parent[:8]))

    def test_checkitems_carry_location(self):
        """检查项节点应尽量带物理位置（用于导航）"""
        total = kgapi.node_count("check_item")
        r = kgdb.query_one(
            "SELECT COUNT(*) AS c FROM kg_node WHERE node_type='check_item' "
            "AND extra IS NOT NULL AND JSON_EXTRACT(extra, '$.location') IS NOT NULL")
        with_loc = r["c"] if r else 0
        self.assertGreater(with_loc, 0, "没有任何检查项带位置信息")
        self.assertGreaterEqual(
            with_loc / float(total or 1), 0.10,
            "带位置的检查项占比 %.2f%% 过低（%d/%d）"
            % (with_loc * 100.0 / (total or 1), with_loc, total))


if __name__ == "__main__":
    unittest.main(verbosity=2)
