# -*- coding: utf-8 -*-
"""
kgapi 调用示例
==============

运行：python tests/kg/fctest/example_kgapi.py

kgapi 是只读查询辅助（查 kg_node / kg_edge / kg_alias 三张表）。
全部只读、不依赖 Django，返回 Python 原生类型。

生产代码不用它 —— 生产走 store.py + recommend.py。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # tests/kg

import kgapi


# ============================================================ 1. 规模统计
print("===== 1. 规模统计 =====")

print("counts()               =", kgapi.counts())
print("node_count()           =", kgapi.node_count())
print("node_count('symptom')  =", kgapi.node_count("symptom"))
print("edge_count()           =", kgapi.edge_count())
print("edge_count('表现为')    =", kgapi.edge_count("表现为"))
print("alias_count()          =", kgapi.alias_count())
print("node_types()           =", kgapi.node_types())


# ============================================================ 2. 查节点
print("\n===== 2. 查节点 =====")

node = kgapi.get_node("symptom:腰痛")
print("get_node('symptom:腰痛') =")
print(json.dumps(node, ensure_ascii=False, indent=4))

print("node_exists('symptom:腰痛')   =", kgapi.node_exists("symptom:腰痛"))
print("node_exists('symptom:不存在')  =", kgapi.node_exists("symptom:不存在"))

# 同名可能同时有 symptom 和 disease 两个节点（647 个名字是这样）
pairs = kgapi.get_nodes_by_name("腰痛")
print("get_nodes_by_name('腰痛') =")
for n in pairs:
    print("    ", n["node_key"], "|", n["node_type"])

names = [n["name"] for n in kgapi.all_nodes("department", limit=5)]
print("all_nodes('department', limit=5) =", names)


# ============================================================ 3. 查别名
print("\n===== 3. 查别名 =====")

print("alias_names('腿疼')    =", kgapi.alias_names("腿疼"))
print("alias_names('拉肚子')  =", kgapi.alias_names("拉肚子"))
print("alias_names('崴脚')    =", kgapi.alias_names("崴脚"))
print("alias_targets('腿疼')  =", kgapi.alias_targets("腿疼"))

counts = kgapi.alias_target_counts()
single = sum(1 for c in counts.values() if c == 1)
print("alias_target_counts() = %d 个口语表达，其中 %d 个只指向 1 个节点"
      % (len(counts), single))
print("    指向最多的 5 个    =", sorted(counts.items(), key=lambda x: -x[1])[:5])

rows = kgapi.all_aliases_row()
print("all_aliases_row()     = %d 行" % len(rows))
print("    样例：")
for r in [x for x in rows if "…" not in x["alias"]][:3]:
    print("        %-8s -> %s" % (r["alias"], r["node_key"]))


# ============================================================ 4. 查边
print("\n===== 4. 查边 =====")

print("in_edges('symptom:腰痛', relation='表现为') =")
for e in kgapi.in_edges("symptom:腰痛", relation="表现为", limit=5):
    print("    %-26s -[%s]-> %s" % (e["src_key"], e["relation"], e["dst_key"]))
print("    共 %d 条" % len(kgapi.in_edges("symptom:腰痛", relation="表现为")))

depts = [e["dst_key"] for e in
         kgapi.out_edges("disease:腰椎间盘突出", relation="就诊于")]
print("out_edges('disease:腰椎间盘突出', relation='就诊于') =", depts)

print("out_edges('disease:腰椎间盘突出') 全部出边 =")
for e in kgapi.out_edges("disease:腰椎间盘突出", limit=6):
    print("    %-10s -> %s" % (e["relation"], e["dst_key"]))


# ============================================================ 5. 科室
print("\n===== 5. 科室 =====")

depts = kgapi.department_names()
print("department_names()      = %d 个，前 5 个 %s" % (len(depts), depts[:5]))

kids = kgapi.pediatric_departments()
print("pediatric_departments() = %d 个，前 3 个 %s" % (len(kids), kids[:3]))


# ============================================================ 6. 组合写法
#
#   # 这个病挂哪个科
#   [e["dst_key"] for e in kgapi.out_edges("disease:腰椎间盘突出", relation="就诊于")]
#
#   # 说这个词会被理解成什么
#   kgapi.alias_names("腿疼")
#
#   # 这个症状关联哪些病
#   [e["src_key"] for e in kgapi.in_edges("symptom:腰痛", relation="表现为")]
#
#   # 先查数据在不在，再排查问题
#   kgapi.counts()
#
#   # 想写自己的 SQL，用底层的 db
#   import db
#   db.query("SELECT * FROM kg_node LIMIT 5")                          # list[dict]
#   db.query_one("SELECT * FROM kg_node WHERE name=%s", ("腰痛",))      # dict 或 None
#
