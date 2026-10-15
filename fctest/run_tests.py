#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
知识图谱测试运行器（功能测试 fctest）
=====================================

统一入口，输出人类可读的测试报告。

用法（在项目根目录执行）：

    python tests/kg/fctest/run_tests.py              # 运行全部测试
    python tests/kg/fctest/run_tests.py -v           # 显示每个测试方法名
    python tests/kg/fctest/run_tests.py -q           # 只显示统计
    python tests/kg/fctest/run_tests.py integrity    # 只跑数据完整性
    python tests/kg/fctest/run_tests.py nodes        # 只跑图谱节点层
    python tests/kg/fctest/run_tests.py --perf       # 附带性能基准（图谱规模 + 检索延迟）

目录约定：
    tests/kg/          图谱程序本身（db/store/agent/recommend/config）
    tests/kg/fctest/   测试程序（本目录），可整体删除而不影响网站运行

退出码：
    0 = 全部通过
    1 = 有失败或错误
    2 = 环境不满足（如缺少数据文件）
"""

import os
import sys
import time
import argparse
import unittest

# 保证中文在 Windows 控制台正常输出
def _force_utf8():
    """
    让中文在 Windows 控制台正常输出。

    ⚠️ 必须用 reconfigure()，不要用 sys.stdout = io.TextIOWrapper(sys.stdout.buffer)。
       后者会在被多次调用时（如测试运行器 + 测试文件都调用）创建多个包装器，
       旧包装器被 GC 时会把底层 buffer 一起关掉，导致
       "ValueError: I/O operation on closed file"。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


_force_utf8()

_HERE = os.path.dirname(os.path.abspath(__file__))            # tests/kg/fctest/
_KG_DIR = os.path.dirname(_HERE)                              # tests/kg/
# 两个都要加：fctest 提供 test_config，tests/kg 提供 paths/db/store/agent/recommend
for _p in (_KG_DIR, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from paths import PROJECT_ROOT as _PROJ                        # noqa: E402

SUITES = {
    "integrity": "test_data_integrity",   # CSV 产物完整性（不依赖数据库）
    "nodes":     "test_kg_nodes",         # MySQL 节点层（依赖数据库）
    "layer2":    "test_kg_layer2",        # 第2层：工具集 + 图遍历 + LLM 选节点
    "llm":       "test_llm_config",       # LLM 配置中心（不需要 Key/网络）
}


def build_suite(which=None):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    names = [which] if which else list(SUITES)
    for key in names:
        mod = SUITES[key]
        try:
            suite.addTests(loader.loadTestsFromName(mod))
        except Exception as e:
            print("⚠️  无法加载测试模块 %s: %s" % (mod, e))
    return suite


def run_perf_baseline():
    """性能基准：图谱规模 + 规则层延迟 + 图遍历延迟（MySQL 版，网站实际走的路径）"""
    print("\n" + "=" * 66)
    print("  性能基准")
    print("=" * 66)

    try:
        import db as kgdb
        from store import get_store
        from agent import get_selector
        from recommend import traverse
    except Exception as e:
        print("  跳过（无法导入模块）: %s" % e)
        return

    # ---- ① 图谱规模 ----
    t0 = time.time()
    counts = kgdb.table_counts()
    db_ms = (time.time() - t0) * 1000

    if any(not isinstance(v, int) for v in counts.values()):
        print("  跳过（数据库不可用）: %s" % counts)
        return

    for name, n in counts.items():
        print("  %-12s : %s 行" % (name, format(n, ",")))
    print("  %-12s : %s 行" % ("合计", format(sum(counts.values()), ",")))
    print("  %-12s : %.1f ms" % ("统计耗时", db_ms))

    try:
        sel = get_selector()
    except Exception as e:
        print("\n  跳过延迟测试（无法构造检索器）: %s" % e)
        return

    # ---- ② 规则层延迟（口语表达精确命中别名表 → 不调 LLM）----
    samples = [
        "腿疼", "我腿疼", "最近牙疼", "眼睛有点疼", "拉肚子",
        "我这两天肚子不舒服", "心口像压了块石头", "睡不着心情低落",
    ]
    sel._rule_match("预热")
    t0, n, hit = time.time(), 0, 0
    for _ in range(20):
        for s in samples:
            if sel._rule_match(s):
                hit += 1
            n += 1
    rule_ms = (time.time() - t0) / n * 1000

    print()
    print("  规则层延迟 : %6.2f ms/次   (%d 次，命中 %d 次)" % (rule_ms, n, hit))
    print("                ↑ 最快的路径，不消耗 LLM，网站大多数口语输入走这里")

    # ---- ③ 图遍历延迟 ----
    seed = sel._rule_match("腿疼") or sel._rule_match("腰痛")
    if seed:
        traverse(seed)                                    # 预热
        t0 = time.time()
        for _ in range(20):
            traverse(seed)
        trav_ms = (time.time() - t0) / 20 * 1000
        print("  图遍历延迟 : %6.2f ms/次   (种子节点 %s)" % (trav_ms, "、".join(seed)))
    else:
        print("  图遍历延迟 :   跳过（未找到种子节点）")

    # ---- ④ 工具层延迟 ----
    store = get_store()
    store.search_nodes("肚子疼", limit=5)                 # 预热
    t0 = time.time()
    for _ in range(20):
        store.search_nodes("肚子疼", limit=5)
    tool_ms = (time.time() - t0) / 20 * 1000
    print("  工具层延迟 : %6.2f ms/次   (search_nodes)" % tool_ms)

    print()
    print("  LLM 层参考 : 约 9~10 秒/次（工具调用多轮往返，需真实 API）")
    print("                用 python tests/kg/demo.py --llm 观察")


def main():
    ap = argparse.ArgumentParser(
        description="知识图谱测试运行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python tests/kg/fctest/run_tests.py\n"
               "  python tests/kg/fctest/run_tests.py -v integrity\n"
               "  python tests/kg/fctest/run_tests.py --perf\n"
    )
    ap.add_argument("which", nargs="?", choices=list(SUITES),
                    help="只运行指定测试集（默认全部）")
    ap.add_argument("-v", "--verbose", action="store_true", help="显示每个测试方法")
    ap.add_argument("-q", "--quiet", action="store_true", help="只显示统计")
    ap.add_argument("--perf", action="store_true", help="附加性能基准")
    ap.add_argument("--live", action="store_true",
                    help="允许调用真实 LLM 的用例（layer2 测试集用）")
    args = ap.parse_args()

    if args.quiet:
        verbosity = 0
    elif args.verbose:
        verbosity = 2
    else:
        verbosity = 1

    print("=" * 66)
    print("  知识图谱测试套件")
    print("=" * 66)
    print("  项目根目录 : %s" % _PROJ)
    print("  测试集     : %s" % (args.which or "全部 (%s)" % ", ".join(SUITES)))
    print()

    suite = build_suite(args.which)
    if suite.countTestCases() == 0:
        print("❌ 没有加载到任何测试（可能缺少数据文件）")
        return 2

    # layer2 的 LLM 用例通过检查 sys.argv 里的 --live 决定是否执行
    if args.live and "--live" not in sys.argv:
        sys.argv.append("--live")

    t0 = time.time()
    runner = unittest.TextTestRunner(verbosity=verbosity, buffer=False)
    result = runner.run(suite)
    elapsed = time.time() - t0

    if args.perf:
        run_perf_baseline()

    print()
    print("=" * 66)
    total = result.testsRun
    failed = len(result.failures)
    errored = len(result.errors)
    skipped = len(result.skipped)
    passed = total - failed - errored - skipped
    print("  测试总数 : %d" % total)
    print("  ✅ 通过  : %d" % passed)
    if failed:
        print("  ❌ 失败  : %d" % failed)
    if errored:
        print("  ⚠️  错误  : %d" % errored)
    if skipped:
        print("  ⏭️  跳过  : %d" % skipped)
    print("  耗时     : %.2f 秒" % elapsed)
    print("=" * 66)

    if failed or errored:
        print("\n提示：失败详情见上方输出。常见原因：")
        print("  * 数据文件过期 → 重新运行 python tests/kg/builders/build_kg.py")
        print("  * 科室映射有误 → 检查 tests/kg/builders/build_kg.py 的 DEPARTMENT_MAP")
        print("  * 同义词死链   → 检查 data/kg/symptom_synonyms.csv 的规范术语列")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
