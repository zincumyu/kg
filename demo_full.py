#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
知识图谱能力演示
================

一个可以直接运行的演示脚本，展示知识图谱**实际能做什么**、**怎么工作的**、
以及**边界在哪**。所有输出都来自真实数据库，不是模拟数据。

用法（在项目根目录或本目录执行）：

    python tests/kg/demo_full.py                  # 跑全部演示
    python tests/kg/demo_full.py --quick          # 只看核心链路（快）
    python tests/kg/demo_full.py --ask 我腿疼      # 只查一个输入
    python tests/kg/demo_full.py --stats          # 只看数据规模
    python tests/kg/demo_full.py --ask 我腿疼 --llm  # 走 LLM 层（复杂表达）

前置条件：
    * MySQL 容器在运行（docker ps 能看到 medical-mysql）
    * 已导入数据（python tests/kg/builders/import_kg_nodes.py）

演示分五部分：
    ① 数据总览       —— 图里有什么
    ② 节点与边        —— 图是怎么组织的
    ③ 文本→节点       —— 患者口语怎么变成节点
    ④ 端到端          —— 完整推荐链路（含推理依据）
    ⑤ 能力边界        —— 现在做不到什么
"""

import os
import sys
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import db as kgdb                                           # noqa: E402
import kgapi                                                # noqa: E402
from store import get_store                                 # noqa: E402
from recommend import traverse                             # noqa: E402
from agent import get_selector                              # noqa: E402

_DB_OK, _DB_DESC = kgdb.health()
if not _DB_OK:
    print("=" * 72)
    print("无法连接数据库")
    print("=" * 72)
    print("错误: %s" % _DB_DESC)
    print()
    print("请检查：")
    print("  1. MySQL 容器是否在运行:  docker ps | findstr medical-mysql")
    print("  2. 是否已导入数据:       python tests/kg/builders/import_kg_nodes.py")
    sys.exit(2)

W = 72
TYPE_CN = {"disease": "疾病", "symptom": "症状", "check_item": "检查项",
           "doctor": "医生", "department": "科室"}
_node_cache = {}


def title(n, text):
    print()
    print("=" * W)
    print("  %s. %s" % (n, text))
    print("=" * W)


def sub(text):
    print()
    print("▸ %s" % text)
    print("-" * W)


def node_name(key):
    """node_key → 可读名字"""
    if key not in _node_cache:
        row = kgapi.get_node(key)
        _node_cache[key] = (row["name"] if row else key,
                            row["node_type"] if row else "")
    return _node_cache[key]


def node_label(key, with_type=True):
    name, ntype = node_name(key)
    if not with_type or ntype == "department":
        return name
    return "%s[%s]" % (name, TYPE_CN.get(ntype, ntype))


# ======================================================================
def demo_overview():
    title(1, "数据总览 —— 图里有什么")

    nodes = kgapi.node_count()
    edges = kgapi.edge_count()
    aliases = kgapi.alias_count()
    print("  节点 %s 个    边 %s 条    别名 %s 条"
          % (f"{nodes:,}", f"{edges:,}", f"{aliases:,}"))

    sub("节点类型分布")
    for t, c in sorted(kgapi.node_types().items(), key=lambda x: -x[1]):
        print("    %-10s %8s" % (TYPE_CN.get(t, t), f"{c:,}"))

    sub("关系类型分布")
    for r in kgdb.query("SELECT relation, COUNT(*) AS c FROM kg_edge "
                        "GROUP BY relation ORDER BY c DESC"):
        print("    %-10s %8s" % (r["relation"], f"{r['c']:,}"))

    sub("数据来源与许可")
    print("    医学知识  : OpenCMKG (github.com/RuiqingDing/OpenCMKG)")
    print("    医院数据  : 排班表 / 科室检查项目（内部）")
    print("    ⚠️ 许可    : OpenCMKG 仅限学术研究，不得商用")
    print("    ⚠️ 免责    : 本图谱用于导诊参考，不构成医疗诊断建议")


# ======================================================================
def demo_graph_structure():
    title(2, "节点与边 —— 图是怎么组织的")

    print("""
  设计：所有实体统一进 kg_node，用 node_key = "类型:名称" 做唯一标识

      症状 ──表现为──▶ 疾病 ──就诊于──▶ 科室
                        │
                        ├──需要检查──▶ 检查项
                        └──并发症──▶ 疾病
""")

    sub("看一个节点：症状「呼吸困难」")
    n = kgapi.get_node("symptom:呼吸困难") or (kgapi.all_nodes("symptom", 1) or [None])[0]
    if not n:
        print("    (无数据)")
        return
    print("    node_key  : %s" % n["node_key"])
    print("    node_type : %s" % n["node_type"])
    print("    name      : %s" % n["name"])
    print("    extra     : %s" % (n.get("extra") or "无"))
    print("    别名      : %d 条" % len(kgdb.query(
        "SELECT alias FROM kg_alias WHERE node_key=%s LIMIT 100", (n["node_key"],))))
    dis = kgdb.query("SELECT src_key FROM kg_edge WHERE dst_key=%s "
                     "AND relation='表现为' LIMIT 4", (n["node_key"],))
    print("    关联疾病  : %s" % ", ".join(node_label(r["src_key"]) for r in dis))

    sub("看一个科室节点的属性：心血管专科")
    d = kgapi.get_nodes_by_name("心血管专科", "department")
    if d:
        d = d[0]
        print("    node_key : %s" % d["node_key"])
        print("    extra    : %s" % d.get("extra"))
        r = kgdb.query_one("SELECT COUNT(*) AS c FROM kg_edge WHERE dst_key=%s "
                           "AND relation='就诊于'", (d["node_key"],))
        print("    关联疾病 : %d 个" % r["c"])
        doc = kgdb.query("SELECT src_key FROM kg_edge WHERE dst_key=%s "
                         "AND relation='所属科室' LIMIT 4", (d["node_key"],))
        print("    出诊医生 : %s" % ("、".join(node_label(r["src_key"]) for r in doc) or "无"))


# ======================================================================
LINK_CASES = ["腿疼", "上不来气", "睡眠不好", "拉肚子", "眼睛疼",
              "手心潮热", "腰没有疼", "三天不拉屎"]


def demo_entity_linking(cases):
    title(3, "文本 → 节点 —— 患者口语怎么变成节点")

    print("""
  做法：查 kg_alias 表（口语 → 规范节点），数据来自数据集自带的 SameAs 关系
  关键：输出是「节点」，不是字符串 —— 后续可以继续对节点做遍历
""")

    sub("实测")
    for text in cases:
        r = get_store().search_nodes(text)
        hit = [n for n in r["nodes"] if n.get("match") in ("alias", "name_exact", "chopped")]
        if not hit:
            print("  「%-8s」 → (无命中)" % text)
            continue
        names = [node_label(k["node_key"]) for k in hit[:4]]
        more = "" if len(hit) <= 4 else " 等 %d 个" % len(hit)
        print("  「%-8s」 → %s%s" % (text, "、".join(names), more))

    sub("观察到的现象")
    print("""
  ✅ 「上不来气」→ 呼吸困难     口语规范化生效
  ✅ 「睡眠不好」→ 入睡困难     簇内解析生效
  ✅ 「腰没有疼」→ 无命中       真否定被正确过滤
  ⚠️ 「手心潮热」→ 无命中       该簇未桥接（数据集本身没给它疾病挂钩）
""")


# ======================================================================
ASK_CASES = ["腿疼", "上不来气", "拉肚子", "牙疼", "心口疼"]


def demo_end_to_end(cases, use_llm=False):
    title(4, "端到端：文本 → 科室 —— 完整推荐链路")

    print("""
  链路：

      患者输入
         │  ① NodeSelector 选节点
         │     规则层（别名表精确/剥填充词/切词）→ 命中即返回，10ms
         │     未命中 → LLM 工具调用 → 查图谱选节点，1~2s
         ▼
      症状节点 ──② 反向遍历「表现为」──▶ 疾病节点
                                          │  ③ 遍历「就诊于」
                                          ▼
                                        科室节点
                                          │  ④ 加权投票 + 年龄过滤
                                          ▼
                              推荐科室 + 置信度 + 依据
""")

    sel = get_selector()
    for text in cases:
        sub("输入：「%s」" % text)
        s = sel.select(text, use_llm=use_llm)
        if not s["nodes"]:
            print("    ① 节点选择 : 无命中（来源 %s）" % s["source"])
            if s.get("note"):
                print("       原因: %s" % s["note"][:80])
            if not use_llm:
                print("       提示: 当前是 --no-llm 模式，复杂表达需加 --llm 才能走 LLM 层")
            continue
        print("    ① 节点选择 : %s   [来源 %s, %dms]"
              % ("、".join(node_label(k) for k in s["nodes"][:4]), s["source"], s["elapsed_ms"]))

        r = traverse(s["nodes"], top_n=3)
        deps = r["departments"]
        if not deps:
            print("    ② 图遍历   : 未得到科室")
            continue
        print("    ② 图遍历   : %d 个候选（共 %d 票）%s"
              % (len(deps), r["total_votes"],
                 "  [已过滤儿科: %s]" % "、".join(r["filtered_pediatric"])
                 if r.get("filtered_pediatric") else ""))
        print()
        print("    %-4s %-14s %-9s %s" % ("排名", "科室", "置信度", "依据（投票的疾病）"))
        print("    " + "-" * 60)
        for i, d in enumerate(deps, 1):
            ev = "、".join(d["依据"][:2])
            print("    %-4s %-14s %-9.2f %s" % (i, d["科室"], d["置信度"], ev))
        print()
        if r["enough"]:
            print("    ✅ 推荐挂号：%s" % deps[0]["科室"])
        else:
            print("    ⚠️ 置信度 %.2f 低于阈值 %.2f → 应回答「不确定」"
                  % (r["confidence"], 0.15))


# ======================================================================
def demo_limits():
    title(5, "能力边界 —— 现在做不到什么")

    print("""
  ⚠️ 以下都是真实存在的限制，不是理论问题：

  1. 部分口语无命中
     如「手臂疼痛」「手心潮热」—— 数据集未收录或未桥接。
     影响：这些输入拿不到推荐。
     解决：从 4,221 条真实对话挖掘补充（待开发）。

  2. 否定识别只在别名层
     别名表过滤了「腰没有疼」这类整体表达，但完整句子里的否定还没处理。
     例：「我不头痛，就是有点晕」会同时匹配到「头痛」和「头晕」。
     解决：需在文本匹配层加否定检测（待开发）。

  3. 置信度是相对值
     上面的 0.4 / 0.3 是「该科室得票 / 总票数」，
     不是准确率，也与申请书里的「阈值 0.85」量纲不同，不能混用。

  4. 科室映射未人工核对
     数据集 48 个科室 → 医院 254 个挂号科室，靠一张手工映射表
     （在 builders/build_kg.py 的 DEPARTMENT_MAP）。
     映射错了会推荐到错误科室。需医院/医学人员核对。

  5. 数据集本身有噪声
     少量别名的语义与规范术语相反（如数据集把「小便发白」归到「尿黄」簇）。
     加权投票能吸收一部分，但无法根除。

  6. 年龄未知时默认排除儿科
     traverse(age=None) 会排除儿科科室。这是刻意的策略：
     综合医院成年患者占多数，给成年人推荐儿科是更严重的错误。
     如果主要服务儿科，需要显式传 age。
""")


# ======================================================================
def main():
    ap = argparse.ArgumentParser(description="知识图谱能力演示")
    ap.add_argument("--quick", action="store_true", help="只跑核心链路")
    ap.add_argument("--stats", action="store_true", help="只看数据规模")
    ap.add_argument("--ask", metavar="文本", help="只查询一个输入")
    ap.add_argument("--llm", action="store_true",
                    help="强制走 LLM 层（--ask 已默认开启，保留兼容）")
    ap.add_argument("--no-llm", action="store_true",
                    help="只走规则层，不调用 LLM（快，但复杂表达会无命中）")
    a = ap.parse_args()

    print()
    print("╔" + "═" * (W - 2) + "╗")
    print("║" + "知识图谱能力演示".center(W - 6) + "║")
    print("║" + "所有数据来自真实数据库，非模拟".center(W - 8) + "║")
    print("╚" + "═" * (W - 2) + "╝")

    if a.ask:
        # --ask 默认走完整链路（规则层 + LLM），这样用户看到的才是真实能力。
        # 想只看规则层用 --no-llm。
        demo_end_to_end([a.ask], use_llm=not a.no_llm)
        return 0
    if a.stats:
        demo_overview()
        return 0
    if a.quick:
        demo_overview()
        demo_entity_linking(LINK_CASES[:5])
        demo_end_to_end(ASK_CASES[:3], use_llm=not a.no_llm)
        return 0

    demo_overview()
    demo_graph_structure()
    demo_entity_linking(LINK_CASES)
    demo_end_to_end(ASK_CASES, use_llm=not a.no_llm)
    demo_limits()

    print()
    print("═" * W)
    print("  演示结束。更多用法见 tests/kg/README.md")
    print("═" * W)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
