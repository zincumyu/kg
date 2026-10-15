# -*- coding: utf-8 -*-
"""
数据完整性测试
==============

验证知识图谱**产物文件本身**是否健康。不涉及检索逻辑。

测试覆盖：
    1. 文件存在性
    2. CSV 格式规范
    3. 内容非空
    4. 无异常字符 / 空白 / 重复
    5. 实体数量在合理区间（防止数据源变动导致静默失败）
    6. 科室映射全部可挂号
    7. 许可与免责声明已就位（合规要求）

运行：
    python -m unittest tests.kg.test_data_integrity -v
"""

import os
import re
import io
import collections
import unittest

from test_config import (
    KGTestCase, read_csv, F_SYMPTOM_DISEASE, F_DISEASE_DEPT, F_SYNONYMS,
    F_CYPHER, F_REPORT, F_LICENSE, F_TRIPLES,
)


# 实体数量的合理区间。
# 用途：如果数据源被替换或脚本出错导致数量骤降，这里会立刻报警，
#      而不是等到线上推荐全部失效才发现。
EXPECTED = {
    "症状数":        (4000, 20000),
    "疾病数":        (5000, 20000),
    "症状疾病关系":  (30000, 80000),
    "疾病科室关系":  (5000, 20000),
    "同义词数":      (80, 2000),   # 人工维护；低于 80 说明数据被误删
}


class TestFilesExist(KGTestCase):
    """1. 文件存在性"""

    def test_required_files_exist(self):
        """核心产物文件必须存在"""
        required = [
            F_SYMPTOM_DISEASE, F_DISEASE_DEPT, F_SYNONYMS,
            F_CYPHER, F_REPORT, F_LICENSE,
        ]
        for p in required:
            with self.subTest(file=os.path.basename(p)):
                self.assertTrue(os.path.exists(p), "缺少文件: %s" % p)
                self.assertGreater(os.path.getsize(p), 0, "文件为空: %s" % p)

    def test_source_dataset_exists(self):
        """原始数据集必须存在（用于复现）"""
        self.assertTrue(os.path.exists(F_TRIPLES), "缺少数据集: %s" % F_TRIPLES)


class TestCsvFormat(KGTestCase):
    """2. CSV 格式规范"""

    def _check_header(self, path, expected_cols):
        rows = read_csv(path)
        self.assertNotEmpty(rows, "%s 无数据行" % os.path.basename(path))
        actual = list(rows[0].keys())
        self.assertEqual(
            actual, expected_cols,
            "%s 表头不符。期望 %s，实际 %s" % (os.path.basename(path), expected_cols, actual)
        )

    def test_symptom_disease_header(self):
        """symptom_disease.csv 表头必须是 [症状, 疾病]"""
        self._check_header(F_SYMPTOM_DISEASE, ["症状", "疾病"])

    def test_disease_department_header(self):
        """disease_department.csv 表头必须是 [疾病, 医院科室]"""
        self._check_header(F_DISEASE_DEPT, ["疾病", "医院科室"])

    def test_synonyms_header(self):
        """symptom_synonyms.csv 表头必须是 [口语表达, 规范术语, 备注]"""
        self._check_header(F_SYNONYMS, ["口语表达", "规范术语", "备注"])


class TestNoBadValues(KGTestCase):
    """3. 无异常值（空白、NaN 字符串、超长、控制字符）"""

    # 常见脏数据模式
    BAD_PATTERNS = re.compile(r"^(nan|NaN|None|null|NULL|\\N|)$")

    def _assert_clean(self, name, values):
        for v in values:
            with self.subTest(实体=name, 值=repr(v)[:40]):
                self.assertFalse(
                    self.BAD_PATTERNS.match(v),
                    "%s 含有脏值: %r" % (name, v)
                )

    def test_no_dirty_symptom_names(self):
        """症状名不含 nan / 空白 / None 等脏值"""
        self._assert_clean("症状", self.sd.keys())

    def test_no_dirty_disease_names(self):
        """疾病名不含脏值"""
        self._assert_clean("疾病", self.dd.keys())

    def test_no_dirty_department_names(self):
        """科室名不含脏值"""
        vals = {d for v in self.dd.values() for d in v}
        self._assert_clean("科室", vals)

    def test_entities_have_no_whitespace(self):
        """实体名首尾不能有空白字符（会导致匹配失败）"""
        for name, values in (("症状", self.sd.keys()), ("科室", {d for v in self.dd.values() for d in v})):
            for v in values:
                with self.subTest(类别=name, 值=v):
                    self.assertEqual(v, v.strip(), "%s 含首尾空白: %r" % (name, v))

    def test_no_control_characters(self):
        """实体名不含换行、制表符等控制字符"""
        ctrl = re.compile(r"[\x00-\x1f\x7f]")
        for v in list(self.sd.keys())[:2000] + list(self.dd.keys())[:2000]:
            with self.subTest(值=repr(v)[:40]):
                self.assertIsNone(ctrl.search(v), "含控制字符: %r" % v)

    def test_entity_length_reasonable(self):
        """实体名长度在 2~30 之间（过短是噪声，过长多半是句子）"""
        for v in list(self.sd.keys()) + list(self.dd.keys()):
            with self.subTest(值=repr(v)[:40]):
                self.assertGreaterEqual(len(v), 2, "过短: %r" % v)
                self.assertLessEqual(len(v), 30, "过长: %r" % v)


class TestNoDuplicates(KGTestCase):
    """4. 无重复行（CSV 内不应有完全重复的记录）"""

    def _check_dup(self, path, key_cols):
        rows = read_csv(path)
        seen = collections.Counter(
            tuple((r.get(c) or "").strip() for c in key_cols) for r in rows
        )
        dups = {k: n for k, n in seen.items() if n > 1}
        self.assertFalse(
            dups,
            "%s 有 %d 组重复记录，示例: %s" %
            (os.path.basename(path), len(dups), list(dups.items())[:3])
        )

    def test_symptom_disease_unique(self):
        """症状→疾病 无重复"""
        self._check_dup(F_SYMPTOM_DISEASE, ["症状", "疾病"])

    def test_disease_department_unique(self):
        """疾病→科室 无重复"""
        self._check_dup(F_DISEASE_DEPT, ["疾病", "医院科室"])

    def test_synonyms_unique(self):
        """同义词表无重复的「口语表达」"""
        self._check_dup(F_SYNONYMS, ["口语表达"])


class TestScaleInRange(KGTestCase):
    """5. 实体数量在合理区间（防止数据源变动导致静默失效）"""

    def test_symptom_count_in_range(self):
        lo, hi = EXPECTED["症状数"]
        n = len(self.sd)
        self.assertTrue(lo <= n <= hi, "症状数 %d 超出预期区间 [%d, %d]" % (n, lo, hi))

    def test_disease_count_in_range(self):
        lo, hi = EXPECTED["疾病数"]
        n = len(self.dd)
        self.assertTrue(lo <= n <= hi, "疾病数 %d 超出预期区间 [%d, %d]" % (n, lo, hi))

    def test_relation_count_in_range(self):
        sd_rel = sum(len(v) for v in self.sd.values())
        dd_rel = sum(len(v) for v in self.dd.values())
        for name, n in (("症状疾病关系", sd_rel), ("疾病科室关系", dd_rel)):
            lo, hi = EXPECTED[name]
            with self.subTest(指标=name):
                self.assertTrue(lo <= n <= hi, "%s = %d，超出 [%d, %d]" % (name, n, lo, hi))

    def test_synonym_count_in_range(self):
        lo, hi = EXPECTED["同义词数"]
        n = len(self.syn)
        self.assertTrue(lo <= n <= hi, "同义词数 %d 超出 [%d, %d]" % (n, lo, hi))


class TestReferentialIntegrity(KGTestCase):
    """6. 引用完整性（两个文件之间能对上）"""

    def test_department_mapping_is_mostly_single(self):
        """
        疾病→科室 应主要是「一对一」。
        一对多说明映射表配置有歧义（如「中医科」同时映射到中医内科和针灸专科），
        5.6% 以内可接受，超过 20% 说明 DEPARTMENT_MAP 需要人工核对。
        """
        multi = sum(1 for v in self.dd.values() if len(v) > 1)
        self.assertRatioAbove(
            len(self.dd) - multi, len(self.dd), 0.80,
            "疾病→科室 一对一映射占比"
        )

    def test_no_orphan_disease(self):
        """疾病→科室 里的疾病应能在 症状→疾病 里找到（否则该科室永远推荐不到）"""
        with_symptom = {d for v in self.sd.values() for d in v}
        orphans = set(self.dd.keys()) - with_symptom
        self.assertRatioAbove(
            len(self.dd) - len(orphans), len(self.dd), 0.95,
            "疾病有症状关联的占比（孤儿疾病无法被推荐）"
        )

    def test_all_recommended_depts_bookable(self):
        """
        ⭐ 核心断言：所有推荐的科室必须能在医院排班表里查到。
        如果不通过，说明 DEPARTMENT_MAP 写错了科室名，患者挂不到号。
        """
        recommended = {d for v in self.dd.values() for d in v}
        bookable = self.bookable
        not_bookable = recommended - bookable
        self.assertFalse(
            not_bookable,
            "以下 %d 个科室在排班表中不存在（患者无法挂号）: %s" %
            (len(not_bookable), sorted(not_bookable))
        )

    def test_department_count_reasonable(self):
        """推荐覆盖的科室数应在合理范围（太少说明映射表不全）"""
        n = len({d for v in self.dd.values() for d in v})
        self.assertTrue(20 <= n <= 80, "覆盖科室数 %d 不在 [20, 80] 区间" % n)


class TestSynonymsValid(KGTestCase):
    """7. 同义词表有效性（每条目标词必须真实存在于图谱）"""

    def test_synonym_targets_exist_in_graph(self):
        """
        ⭐ 核心断言：同义词的「规范术语」必须能在 症状→疾病 里找到。
        否则该同义词是死链，永远匹配不到任何疾病。
        """
        dead = {k: v for k, v in self.syn.items() if v not in self.sd}
        self.assertFalse(
            dead,
            "以下 %d 条同义词指向了图谱中不存在的术语: %s" %
            (len(dead), list(dead.items())[:10])
        )

    def test_slang_differs_from_formal(self):
        """口语表达与规范术语不应完全相同（否则是冗余条目）"""
        same = {k: v for k, v in self.syn.items() if k == v}
        # 少量冗余可接受（如「耳鸣→耳鸣」），但不应超过 20%
        self.assertRatioAbove(
            len(self.syn) - len(same), len(self.syn), 0.80,
            "同义词有效转换率"
        )


class TestCompliance(KGTestCase):
    """8. 合规性（学术使用许可与医疗免责声明）"""

    def test_license_doc_exists_and_documents_limit(self):
        """必须存在数据来源与许可说明，且写明「不得商用」"""
        self.assertTrue(os.path.exists(F_LICENSE), "缺少许可说明文件")
        content = io.open(F_LICENSE, encoding="utf-8").read()
        for kw in ("OpenCMKG", "学术研究", "商用"):
            with self.subTest(关键词=kw):
                self.assertIn(kw, content, "许可文件缺少关键词: %s" % kw)

    def test_report_contains_disclaimer(self):
        """质量报告必须包含医疗免责声明"""
        content = io.open(F_REPORT, encoding="utf-8").read()
        self.assertIn("不构成医疗诊断", content, "报告缺少医疗免责声明")

    def test_cypher_has_source_attribution(self):
        """Neo4j 导入脚本必须标注数据来源"""
        head = io.open(F_CYPHER, encoding="utf-8").read(600)
        self.assertIn("OpenCMKG", head, "Cypher 脚本缺少数据来源标注")

    def test_cypher_is_idempotent(self):
        """
        ⭐ 核心断言：导入脚本必须用 MERGE（幂等），
        不能出现 DETACH DELETE（原脚本每次建图都清空数据库）。
        """
        content = io.open(F_CYPHER, encoding="utf-8").read()
        # 去掉注释行后再检查
        code_lines = [l for l in content.splitlines() if not l.strip().startswith("//")]
        code = "\n".join(code_lines)
        self.assertNotIn("DETACH DELETE", code.upper(),
                         "导入脚本含 DETACH DELETE，会清空数据库！")
        self.assertIn("MERGE", code.upper(), "导入脚本未使用 MERGE，不幂等")
        self.assertIn("CREATE CONSTRAINT", code.upper(), "缺少唯一约束，MERGE 性能差")


if __name__ == "__main__":
    unittest.main(verbosity=2)
