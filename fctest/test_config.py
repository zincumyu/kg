# -*- coding: utf-8 -*-
"""
测试通用配置与工具
==================

提供路径常量、临时目录夹具，以及跨测试文件复用的辅助函数。

设计说明
--------
* 只用标准库 `unittest`，不引入 pytest 依赖，保证在任何环境都能跑。
* 测试数据全部从真实产物读取（`data/kg/*.csv`），不做 mock，
  因为本项目的核心风险就是「数据和映射是否正确」，mock 会掩盖问题。
"""

import os
import sys
import csv
import io
import unittest

# ----------------------------------------------------------------------
# 路径
# ----------------------------------------------------------------------
FCTEST_DIR = os.path.dirname(os.path.abspath(__file__))            # tests/kg/fctest/
TESTS_DIR = os.path.dirname(FCTEST_DIR)                            # tests/kg/

# 图谱程序在 tests/kg/，测试在它的子目录，所以把**父目录**加进 sys.path，
# 这样才能 import 到 paths / db / store / agent
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

# 项目根 / 数据目录 / .env 位置统一由 paths.py 算（找标记，不数层数）
from paths import PROJECT_ROOT, DATA_DIR                            # noqa: E402

SERVER_DIR = os.path.join(PROJECT_ROOT, "LLM_server") if PROJECT_ROOT else None

KG_DIR = os.path.join(DATA_DIR, "kg")
OPENCMKG_DIR = os.path.join(DATA_DIR, "opencmkg")
SCHEDULE_XLSX = os.path.join(DATA_DIR, "3.排班表数据.xlsx")

# ----------------------------------------------------------------------
# 数据文件
# ----------------------------------------------------------------------
F_SYMPTOM_DISEASE = os.path.join(KG_DIR, "symptom_disease.csv")
F_DISEASE_DEPT = os.path.join(KG_DIR, "disease_department.csv")
F_SYNONYMS = os.path.join(KG_DIR, "symptom_synonyms.csv")
F_CYPHER = os.path.join(KG_DIR, "kg_import.cypher")
F_REPORT = os.path.join(KG_DIR, "report.md")
F_LICENSE = os.path.join(OPENCMKG_DIR, "数据来源与许可.md")
F_TRIPLES = os.path.join(OPENCMKG_DIR, "triples.txt")


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------
def read_csv(path):
    """读取带 BOM 的 CSV，返回 list[dict]"""
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_symptom_disease():
    """返回 dict: 症状 -> set(疾病)"""
    d = {}
    for r in read_csv(F_SYMPTOM_DISEASE):
        s = (r.get("症状") or "").strip()
        dis = (r.get("疾病") or "").strip()
        if s and dis:
            d.setdefault(s, set()).add(dis)
    return d


def load_disease_department():
    """返回 dict: 疾病 -> set(医院科室)"""
    d = {}
    for r in read_csv(F_DISEASE_DEPT):
        dis = (r.get("疾病") or "").strip()
        dept = (r.get("医院科室") or "").strip()
        if dis and dept:
            d.setdefault(dis, set()).add(dept)
    return d


def load_synonyms():
    """返回 dict: 口语表达 -> 规范术语"""
    d = {}
    for r in read_csv(F_SYNONYMS):
        k = (r.get("口语表达") or "").strip()
        v = (r.get("规范术语") or "").strip()
        if k and v:
            d[k] = v
    return d


def load_bookable_departments():
    """
    读取医院真实可挂号的科室集合（一级 + 二级科室名）。
    需要 pandas + openpyxl。
    """
    import pandas as pd
    df = pd.read_excel(SCHEDULE_XLSX)
    s = set()
    for col in ("yjksmc", "ejksmc"):
        if col in df.columns:
            s |= {str(x).strip() for x in df[col].dropna() if str(x).strip()}
    return s


# ----------------------------------------------------------------------
# 基类
# ----------------------------------------------------------------------
class KGTestCase(unittest.TestCase):
    """
    知识图谱测试基类。

    在 setUpClass 里做一次性数据加载，避免每个测试方法重复读 1.9MB CSV。
    """

    @classmethod
    def setUpClass(cls):
        missing = [p for p in (F_SYMPTOM_DISEASE, F_DISEASE_DEPT) if not os.path.exists(p)]
        if missing:
            raise unittest.SkipTest(
                "缺少知识图谱产物，请先运行: python LLM_server/knowledge/builders/build_kg.py\n"
                "缺失: %s" % ", ".join(missing)
            )
        cls.sd = load_symptom_disease()
        cls.dd = load_disease_department()
        cls.syn = load_synonyms() if os.path.exists(F_SYNONYMS) else {}
        # 惰性缓存医院可挂号科室（首次访问才读 Excel）
        cls._bookable = None

    @property
    def bookable(self):
        """惰性加载医院可挂号科室（首次访问才读 Excel）"""
        cls = type(self)
        if getattr(cls, "_bookable", None) is None:
            try:
                cls._bookable = load_bookable_departments()
            except Exception as e:
                self.skipTest("无法读取排班表，跳过可挂号性检查: %s" % e)
        return cls._bookable

    # -- 断言辅助 --------------------------------------------------------
    def assertNotEmpty(self, obj, msg=None):
        self.assertTrue(obj, msg or "期望非空，实际为空")

    def assertRatioAbove(self, numerator, denominator, min_ratio, msg=""):
        """断言比例达标，失败时打印实际比例便于定位"""
        if denominator == 0:
            self.fail("%s 分母为 0" % msg)
        ratio = numerator / float(denominator)
        self.assertGreaterEqual(
            ratio, min_ratio,
            "%s 实际 %.4f (%d/%d)，要求 >= %.4f" %
            (msg, ratio, numerator, denominator, min_ratio)
        )
