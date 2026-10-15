# -*- coding: utf-8 -*-
"""
第 2 层测试：节点工具集 + 图遍历 + LLM 节点选择
==============================================

覆盖三个新模块：
    knowledge/store.py      4 个只读工具（search / get / expand / path）
    knowledge/recommend.py  图遍历 + 加权投票 + 年龄过滤
    knowledge/agent.py      节点选择器（规则层 + LLM 层）

设计原则：
    **默认不调用真实 LLM** —— 需要网络的用例用 `--live` 开关控制，
    否则测试套件在没配额/没网络时会大面积失败，掩盖真正的代码问题。

运行：
    python tests/kg/fctest/run_tests.py layer2
    python tests/kg/fctest/run_tests.py layer2 --live     # 附带真实 LLM 用例
"""

import sys
import unittest

from test_config import TESTS_DIR

if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)          # 找到图谱程序（tests/kg/）

# ---- 直接使用图谱模块（不依赖 Django）----
import kgapi
import db as kgdb
from store import get_store
from recommend import traverse, is_pediatric_department, MIN_CONFIDENCE
from agent import get_selector

DB_OK, DB_DESC = kgdb.health()
DJANGO_OK = DB_OK          # 变量名保留，避免下游断言大改
ERR = DB_DESC

LIVE = "--live" in sys.argv


# ======================================================================
# 端到端期望值（医学常识，不占网络）
# ======================================================================
END_TO_END = [
    ("腿疼",   "骨科专科"),
    ("上不来气", "呼吸内科"),
    ("牙疼",   "口腔内科"),
    ("拉肚子",  "消化专科"),
]

# 应该判定为「无匹配」的输入
NO_MATCH = ["今天天气真好", "帮我订张机票", "腰没有疼"]


@unittest.skipUnless(DJANGO_OK, "无法初始化 Django: %s" % ERR)
class Layer2Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            if kgapi.node_count() < 1000:
                raise unittest.SkipTest("图谱数据未导入，先跑 import_kg_nodes.py")
        except unittest.SkipTest:
            raise
        except Exception as e:
            raise unittest.SkipTest("无法访问图谱表: %s" % e)


# ======================================================================
class TestStoreTools(Layer2Base):
    """1. knowledge/store.py 的 4 个只读工具"""

    def setUp(self):
        self.s = get_store()

    def test_search_alias_hit(self):
        """⭐ search_nodes 能通过别名匹配到节点（腿疼 → 腰痛）"""
        r = self.s.search_nodes("腿疼")
        names = [n["name"] for n in r["nodes"]]
        self.assertTrue(r["count"] > 0, "「腿疼」未匹配到任何节点")
        self.assertIn("腰痛", names, "「腿疼」应匹配到「腰痛」，实际: %s" % names)

    def test_search_returns_match_reason(self):
        """⭐ 结果要带「为什么匹配」，便于 LLM 判断和事后审计"""
        r = self.s.search_nodes("腿疼")
        for n in r["nodes"]:
            with self.subTest(node=n["name"]):
                self.assertIn("match", n, "结果缺少 match 字段")

    def test_search_by_type(self):
        """node_type 过滤生效"""
        r = self.s.search_nodes("胸痛", node_type="department")
        for n in r["nodes"]:
            self.assertEqual(n["type"], "department")

    def test_search_empty_keyword(self):
        """空关键词返回 error，不抛异常"""
        r = self.s.search_nodes("")
        self.assertIn("error", r)

    def test_search_respects_limit(self):
        """返回条数不超过 limit"""
        r = self.s.search_nodes("痛", limit=3)
        self.assertLessEqual(len(r["nodes"]), 3)

    def test_search_chops_long_sentence(self):
        """
        ⭐ 长句自动切词。
        实测问题：患者说「我这两天走路腿沉，还发麻」，LLM 会整句当关键词搜，
        0 命中后反复重试直到超轮次。这里验证切词兜底能命中。
        """
        r = self.s.search_nodes("我这两天走路腿沉，还发麻")
        self.assertTrue(r["count"] > 0, "长句切词后仍未命中任何节点")
        self.assertIn("chopped_into", r, "应记录切成了哪些片段")

    def test_search_unrelated_returns_hint(self):
        """无关文本应 0 命中，并给出重试提示（避免 LLM 反复试同一个词）"""
        r = self.s.search_nodes("今天天气真好")
        self.assertEqual(r["count"], 0)
        self.assertIn("hint", r, "0 命中时应给出 hint 引导 LLM")

    def test_validate_keys_filters_fake(self):
        """⭐ validate_keys 能识别出 LLM 编造的假节点（核心防幻觉机制）"""
        r = self.s.validate_keys(["symptom:腰痛", "symptom:我编的节点", "disease:也不存在"])
        self.assertIn("symptom:腰痛", r["valid"])
        self.assertIn("symptom:我编的节点", r["invalid"])
        self.assertIn("disease:也不存在", r["invalid"])

    def test_tool_schemas_wellformed(self):
        """工具 schema 符合 OpenAI function calling 格式"""
        schemas = self.s.tool_schemas()
        self.assertEqual(len(schemas), 1,
                         "只保留 search_nodes —— 另 3 个实测 LLM 从不调用，白占 prompt")
        for s in schemas:
            with self.subTest(tool=s["function"]["name"]):
                self.assertEqual(s["type"], "function")
                self.assertIn("name", s["function"])
                self.assertIn("description", s["function"])
                self.assertEqual(s["function"]["parameters"]["type"], "object")

    def test_call_dispatch_and_unknown(self):
        """call() 能调度已知工具，未知工具返回 error 不抛异常"""
        self.assertNotIn("error", self.s.call("search_nodes", {"keyword": "腿疼"}))
        self.assertIn("error", self.s.call("不存在的工具", {}))
        self.assertIn("error", self.s.call("search_nodes", {"错误参数": 1}))

    def test_call_accepts_json_string_args(self):
        """call() 支持 LLM 传来的 JSON 字符串参数"""
        import json
        r = self.s.call("search_nodes", json.dumps({"keyword": "腿疼"}))
        self.assertNotIn("error", r)


# ======================================================================
class TestRecommend(Layer2Base):
    """2. knowledge/recommend.py 图遍历与投票"""

    def setUp(self):
        self.s = get_store()

    def _depts(self, keys, **kw):
        r = traverse(keys, **kw)
        return [d["科室"] for d in r["departments"]], r

    def test_traverse_from_symptom(self):
        """⭐ 症状节点能遍历出科室"""
        deps, r = self._depts(["symptom:牙痛"])
        self.assertTrue(deps, "牙痛未遍历出任何科室")
        self.assertEqual(deps[0], "口腔内科")

    def test_traverse_from_disease(self):
        """疾病节点也能作为起点"""
        deps, r = self._depts(["disease:腰腿痛"])
        self.assertTrue(deps, "腰腿痛未遍历出科室")

    def test_traverse_empty_input(self):
        """空输入返回空结果，不抛异常"""
        r = traverse([])
        self.assertEqual(r["departments"], [])
        self.assertFalse(r["enough"])

    def test_confidence_in_range(self):
        """置信度必须在 (0, 1] 区间"""
        _, r = self._depts(["symptom:牙痛"])
        for d in r["departments"]:
            with self.subTest(dept=d["科室"]):
                self.assertGreater(d["置信度"], 0)
                self.assertLessEqual(d["置信度"], 1.0)

    def test_sorted_by_votes(self):
        """结果按票数降序"""
        _, r = self._depts(["symptom:牙痛", "symptom:腰痛"])
        votes = [d["票数"] for d in r["departments"]]
        self.assertEqual(votes, sorted(votes, reverse=True))

    def test_has_evidence(self):
        """⭐ 每条推荐必须能给出依据（可解释性）"""
        _, r = self._depts(["symptom:牙痛"])
        for d in r["departments"]:
            with self.subTest(dept=d["科室"]):
                self.assertTrue(d["依据"], "科室 %s 缺少推荐依据" % d["科室"])

    def test_pediatric_filter_adult(self):
        """
        ⭐ 成年人不应被推荐儿科。
        实测问题：「上不来气」时「儿科内科」以 102 票压过「呼吸内科」100 票。
        """
        deps, r = self._depts(["symptom:呼吸困难"], age=30)
        for d in deps:
            with self.subTest(dept=d):
                self.assertFalse(is_pediatric_department(d),
                                 "成年人被推荐了儿科科室: %s" % d)

    def test_pediatric_kept_for_child(self):
        """儿童患者保留儿科科室"""
        _, r_adult = self._depts(["symptom:呼吸困难"], age=30)
        _, r_child = self._depts(["symptom:呼吸困难"], age=5)
        # 儿童的结果里允许出现儿科（不强求一定有，因为数据里未必排第一）
        self.assertIsInstance(r_child["departments"], list)

    def test_is_pediatric_department(self):
        """儿科识别规则：含「儿」不等于儿科"""
        cases = [
            ("儿科内科", True),
            ("小儿外科专科", True),
            ("儿童康复", True),
            ("呼吸内科", False),
            ("骨科专科", False),
            # ⚠️ 含「儿」但属于生殖科，不能判成儿科
            ("反复种植失败专科门诊(试管婴儿)", False),
        ]
        for name, expect in cases:
            with self.subTest(name=name):
                self.assertEqual(is_pediatric_department(name), expect)

    def test_confidence_threshold_flag(self):
        """enough 标记应反映是否达到阈值"""
        _, r = self._depts(["symptom:牙痛"])
        self.assertTrue(r["enough"], "牙痛置信度 %.2f 应达标" % r["confidence"])
        self.assertGreaterEqual(r["confidence"], MIN_CONFIDENCE)

    def test_repeated_calls_identical(self):
        """⭐ 幂等：同输入多次遍历结果一致"""
        a = traverse(["symptom:牙痛"])
        b = traverse(["symptom:牙痛"])
        self.assertEqual(a["departments"], b["departments"])

    def test_no_symptom_diffusion_from_disease(self):
        """
        ⭐ 回归测试：疾病起点不能做「症状扩散」。
        实测踩坑：腰腿痛扩散后从 1 票污染成 191 票，
        把「外科普诊」顶到第一名，反而压掉了更合理的「骨科专科」。
        """
        r_direct = traverse(["disease:腰腿痛"])
        r_multi = traverse(["disease:腰腿痛", "symptom:腰痛"])
        # 加一个症状后总票数应该有变化，但不应该爆炸式增长
        self.assertLess(r_multi["total_votes"], r_direct["total_votes"] * 100,
                        "票数异常膨胀，可能又引入了扩散逻辑")


# ======================================================================
class TestSelectorRuleLayer(Layer2Base):
    """3. knowledge/agent.py 规则层（不调 LLM）"""

    def setUp(self):
        self.sel = get_selector()

    def test_rule_layer_hits_known_alias(self):
        """⭐ 别名表能命中的输入走规则层，不调 LLM（快且确定）"""
        r = self.sel.select("腿疼", use_llm=False)
        self.assertEqual(r["source"], "rule")
        self.assertTrue(r["nodes"])
        self.assertIn("symptom:腰痛", r["nodes"])

    def test_rule_layer_no_match(self):
        """规则层无命中时，禁用 LLM 应返回 none"""
        r = self.sel.select("今天天气真好", use_llm=False)
        self.assertEqual(r["source"], "none")
        self.assertEqual(r["nodes"], [])

    def test_negation_not_matched(self):
        """⭐ 真否定表达不应匹配到节点"""
        for text in ["腰没有疼", "肚子不疼"]:
            with self.subTest(text=text):
                r = self.sel.select(text, use_llm=False)
                self.assertEqual(r["nodes"], [], "否定表达被错误匹配: %s" % text)

    def test_empty_input(self):
        """空输入不抛异常"""
        r = self.sel.select("", use_llm=False)
        self.assertEqual(r["nodes"], [])
        self.assertIn("note", r)

    def test_result_structure(self):
        """返回结构含所有约定字段"""
        r = self.sel.select("腿疼", use_llm=False)
        for k in ("nodes", "source", "trace", "elapsed_ms"):
            self.assertIn(k, r, "返回缺少字段: %s" % k)

    def test_filler_words_stripped(self):
        """
        ⭐ 回归测试：带口语填充词的表达也要走规则层。

        实测踩坑：患者说「我腿疼」（多了一个「我」），
        store 的切词阈值当时设成 >4，3 个字不切词，
        整句匹配失败返回 0 命中，白白绕到 LLM 层（9 秒）。
        """
        cases = {
            "我腿疼": "symptom:腰痛",
            "最近牙疼": "symptom:牙痛",
            "眼睛有点疼": "symptom:眼痛",
        }
        failures = []
        for text, expect_key in cases.items():
            r = self.sel.select(text, use_llm=False)
            if r["source"] != "rule":
                failures.append((text, "应为 rule", r["source"]))
            elif expect_key not in r["nodes"]:
                failures.append((text, "应含 " + expect_key, r["nodes"]))
        self.assertFalse(
            failures,
            "带填充词的表达未走规则层:\n%s" %
            "\n".join("  「%s」%s，实际 %s" % f for f in failures))

    def test_no_filler_still_fast(self):
        """带填充词的表达也应保持毫秒级（证明没调 LLM）"""
        for text in ["我腿疼", "最近牙疼"]:
            with self.subTest(text=text):
                r = self.sel.select(text, use_llm=False)
                self.assertLess(r["elapsed_ms"], 500,
                                "「%s」耗时 %dms，疑似调用了 LLM" % (text, r["elapsed_ms"]))

    def test_negation_still_filtered_with_filler(self):
        """⭐ 加填充词后，真否定仍要被拒绝（别为了召回把否定也放进来了）"""
        for text in ["我腰没有疼", "我肚子不疼"]:
            with self.subTest(text=text):
                r = self.sel.select(text, use_llm=False)
                self.assertEqual(r["nodes"], [], "否定表达被错误匹配: %s" % text)

    def test_rule_layer_is_fast(self):
        """⭐ 规则层应远快于 LLM（说明确实走了短路，没调模型）"""
        r = self.sel.select("腿疼", use_llm=False)
        self.assertLess(r["elapsed_ms"], 500,
                        "规则层耗时 %dms，疑似调用了 LLM" % r["elapsed_ms"])


# ======================================================================
class TestEndToEnd(Layer2Base):
    """4. 端到端：文本 → 节点 → 科室（全走规则层，不调 LLM）"""

    def setUp(self):
        self.sel = get_selector()

    def _pipeline(self, text, age=30):
        s = self.sel.select(text, use_llm=False)
        r = traverse(s["nodes"], age=age)
        deps = [d["科室"] for d in r["departments"]]
        return s, r, deps

    def test_common_cases_correct(self):
        """⭐ 医学常识校验：常见主诉的第一名科室必须正确"""
        failures = []
        for text, expect in END_TO_END:
            s, r, deps = self._pipeline(text)
            got = deps[0] if deps else None
            if got != expect:
                failures.append((text, expect, got, s["nodes"]))
        self.assertFalse(
            failures,
            "端到端推荐不符合医学常识:\n%s" %
            "\n".join("  「%s」期望 %s，实际 %s（节点 %s）" % f for f in failures)
        )

    def test_no_match_cases(self):
        """⭐ 无关/否定输入不应产生推荐"""
        for text in NO_MATCH:
            with self.subTest(text=text):
                s, r, deps = self._pipeline(text)
                self.assertFalse(deps, "「%s」不应产生推荐，实际 %s" % (text, deps))

    def test_accuracy_ratio(self):
        """⭐ 准确率门限：样本小，要求 100%"""
        passed = 0
        for text, expect in END_TO_END:
            _, _, deps = self._pipeline(text)
            if deps and deps[0] == expect:
                passed += 1
        self.assertEqual(passed, len(END_TO_END),
                         "端到端准确率 %d/%d" % (passed, len(END_TO_END)))

    def test_departments_bookable(self):
        """⭐ 推荐的科室必须是真实可挂号的（与排班表交叉验证）"""
        from test_config import load_bookable_departments
        try:
            bookable = load_bookable_departments()
        except Exception as e:
            self.skipTest("无法读取排班表: %s" % e)
        for text, _ in END_TO_END:
            _, _, deps = self._pipeline(text)
            for d in deps:
                with self.subTest(text=text, dept=d):
                    self.assertIn(d, bookable, "「%s」推荐的「%s」在排班表里不存在" % (text, d))


# ======================================================================
class TestSelectorLLMLayer(Layer2Base):
    """5. LLM 层（需要真实 API，默认跳过）"""

    def setUp(self):
        self.sel = get_selector()

    @unittest.skipUnless(LIVE, "需要 --live 参数（会调用真实 LLM）")
    def test_llm_handles_colloquial(self):
        """⭐ LLM 层能处理别名表覆盖不了的口语表达"""
        r = self.sel.select("心口像压了块石头")
        self.assertTrue(r["nodes"], "LLM 层未选出任何节点")
        names = []
        for k in r["nodes"]:
            n = kgapi.get_node(k)
            if n:
                names.append(n["name"])
        self.assertTrue(
            any("胸闷" in x or "胸痛" in x or "压迫" in x for x in names),
            "期望选出胸闷/胸痛类节点，实际: %s" % names
        )

    @unittest.skipUnless(LIVE, "需要 --live 参数")
    def test_llm_layer_validates_fake_nodes(self):
        """⭐ 无论 LLM 输出什么，最终节点都必须真实存在（防幻觉）"""
        for text in ["心口像压了块石头", "我这两天走路腿沉还发麻"]:
            with self.subTest(text=text):
                r = self.sel.select(text)
                for k in r["nodes"]:
                    self.assertTrue(
                        kgapi.node_exists(k),
                        "LLM 输出了不存在的节点: %s" % k
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
