# -*- coding: utf-8 -*-
"""
构建医学知识图谱数据（OpenCMKG → 本项目图谱）

用法（在 LLM_server 目录下执行）：

    python knowledge/builders/build_kg.py                 # 只生成产物文件，不连数据库
    python knowledge/builders/build_kg.py --verify        # 生成后打印质量报告
    python knowledge/builders/build_kg.py --load-neo4j    # 额外导入 Neo4j（需要 Neo4j 在跑）

产物（输出到 data/kg/）：
    1. symptom_disease.csv          症状 → 疾病
    2. disease_department.csv       疾病 → 科室（已映射到医院真实挂号科室）
    3. department_map.csv           数据集科室 → 医院科室 的映射明细（供人工核对）
    4. kg_import.cypher             Neo4j 导入脚本（幂等 MERGE）
    5. report.md                    质量报告

为什么要有这个脚本：
    原建图脚本 aichat/ingest_data.py 里的症状-疾病-科室关系是 6 条硬编码元组，
    导致图谱几乎没有医学知识。本脚本用公开数据集（OpenCMKG，35 万三元组）
    替换那 6 条，并映射到医院真实的挂号科室。

数据来源与许可：见 data/opencmkg/数据来源与许可.md（仅限学术研究）
"""

import os
import sys
import io
import csv
import re
import argparse
import collections

# ---------------- 路径 ----------------
BASE = os.path.dirname(os.path.abspath(__file__))          # tests/kg/builders/
PROJ = os.path.dirname(os.path.dirname(os.path.dirname(BASE)))   # 项目根                                          # 项目根
SRC_TRIPLES = os.path.join(PROJ, "data", "opencmkg", "triples.txt")
SCHEDULE_XLSX = os.path.join(PROJ, "data", "3.排班表数据.xlsx")
OUT_DIR = os.path.join(PROJ, "data", "kg")

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


# =====================================================================
# 科室映射表：OpenCMKG 的 48 个科室 → 绵阳市中心医院真实挂号科室（ejksmc）
# =====================================================================
# 设计说明：
#   * 数据集科室是「临床语义科室」（如 心内科/消化内科），医院用的是「挂号科室名」
#     （如 心血管专科/消化专科），两者必须映射，否则推荐出来的科室患者挂不到号。
#   * 右侧取值来自 data/3.排班表数据.xlsx 的 ejksmc 列，均已确认有真实排班。
#   * 映射不唯一时取占用号源最多的科室，保证推荐结果可挂号。
#   * 值写成 list 表示该数据集科室对应多个医院科室（例如中医综合）。
#
# ⚠️ 这个表需要人工核对！请务必对照医院实际科室调整。
DEPARTMENT_MAP = {
    "心内科":       ["心血管专科"],
    "消化内科":     ["消化专科"],
    "呼吸内科":     ["呼吸内科"],
    "神经内科":     ["神经内科"],
    "内分泌科":     ["内分泌代谢专科"],
    "肾内科":       ["肾病内科"],
    "血液科":       ["血液病"],
    "风湿免疫科":   ["风湿免疫科"],
    "感染科":       ["感染疾病科"],
    "传染科":       ["感染疾病科"],
    "肝病":         ["乙肝临床治愈门诊"],

    "普外科":       ["外科普诊"],
    "骨外科":       ["骨科专科"],
    "泌尿外科":     ["泌尿专科"],
    "泌尿内科":     ["泌尿专科"],
    "神经外科":     ["神经外科专科"],
    "心胸外科":     ["心胸外科专科"],
    "肝胆外科":     ["肝胆胰脾外科"],
    "肛肠科":       ["胃肠胰脾外科"],
    "烧伤科":       ["外科普诊"],
    "整形美容科":   ["医疗美容外科"],
    "肿瘤科":       ["肿瘤专科"],
    "肿瘤内科":     ["肿瘤专科"],
    "肿瘤外科":     ["肿瘤专科"],

    "妇科":         ["妇科专科"],
    "产科":         ["产科专科"],
    "不孕不育":     ["不孕症门诊"],
    "生殖健康":     ["生殖健康与不孕门诊"],
    "男科":         ["生殖健康与男科"],

    "儿科综合":     ["儿科内科"],
    "小儿内科":     ["儿科内科"],
    "小儿外科":     ["小儿外科专科"],

    "眼科":         ["眼科专科"],
    "耳鼻喉科":     ["耳鼻喉科"],
    "五官科":       ["耳鼻喉科"],          # 五官科合并了眼/耳鼻喉，取耳鼻喉
    "口腔科":       ["口腔内科"],
    "皮肤科":       ["皮肤病与性病科"],
    "性病科":       ["皮肤病与性病科"],
    "精神科":       ["心理卫生专科"],
    "心理科":       ["心理卫生专科"],
    "康复科":       ["康复科"],
    "中医综合":     ["中医内科", "针灸专科"],
    "营养科":       ["健康体检"],          # 医院无独立营养科，退到健康体检
    "减肥":         ["体重管理(全程管理)"],
    "急诊科":       ["全科医学专科"],      # 医院无急诊挂号，退到全科
    "内科":         ["全科医学专科"],      # 数据集里的泛内科，退到全科
    "其他综合":     ["全科医学专科"],
    "遗传病科":     ["全科医学专科"],
}


# =====================================================================
# 数据质量过滤规则
# =====================================================================
# 噪声症状特征：单字、纯数字、含异常字符等（如数据集里把"毓卓"当症状）
_ILLEGAL = re.compile(r"[0-9A-Za-z\s,;:!?。，；：！？\"'()（）\[\]{}<>/\\|@#$%^&*+=~`]")


def is_valid_term(s: str) -> bool:
    """判断实体名是不是干净的中文医学词"""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 2 or len(s) > 30:      # 单字多半是噪声
        return False
    if _ILLEGAL.search(s):             # 含数字/字母/标点
        return False
    if s.isdigit():
        return False
    return True


# =====================================================================
# 解析 triples.txt
# =====================================================================
def parse_triples(path):
    """
    返回 (dex_sym, dex_dep, dex_check, stats)

    注意：数据集用逗号分隔，但部分实体名内部含逗号，导致列数 != 3。
    这些行直接丢弃（约 2171 行 / 0.6%），不尝试猜测修复。
    """
    dex_sym = collections.defaultdict(set)     # 疾病 -> {症状}
    dex_dep = collections.defaultdict(set)     # 疾病 -> {数据集科室}
    dex_check = collections.defaultdict(set)   # 疾病 -> {检查项}
    stats = collections.Counter()

    with io.open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            stats["总行数"] += 1
            parts = [p.strip() for p in line.rstrip("\n").split(",")]
            if len(parts) != 3:
                stats["跳过_列数异常"] += 1
                continue
            h, r, t = parts
            if not h or not t:
                stats["跳过_空实体"] += 1
                continue

            if r == "disease_has_symptom":
                dex_sym[h].add(t)
            elif r == "disease_belong_department":
                dex_dep[h].add(t)
            elif r == "disease_need_check":
                dex_check[h].add(t)
            else:
                stats["跳过_其他关系"] += 1

    return dex_sym, dex_dep, dex_check, stats


# =====================================================================
# 生成产物
# =====================================================================
def build(verify=False, load_neo4j=False):
    if not os.path.exists(SRC_TRIPLES):
        print("错误: 找不到数据集 %s" % SRC_TRIPLES)
        print("请先按 dev_h/09 文档的说明下载 OpenCMKG 到 data/opencmkg/")
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    print("=" * 68)
    print("  构建医学知识图谱数据")
    print("=" * 68)
    print("数据源: %s" % SRC_TRIPLES)

    dex_sym, dex_dep, dex_check, stats = parse_triples(SRC_TRIPLES)
    print("\n[1/5] 解析完成")
    for k, v in sorted(stats.items()):
        print("    %-18s %8d" % (k, v))

    # ---------- 过滤 + 拼接 ----------
    print("\n[2/5] 过滤噪声、拼接 症状→疾病→科室")
    unmapped = collections.Counter()
    sym_disease = []        # (症状, 疾病)
    dis_dept = []           # (疾病, 医院科室)
    filtered_sym = 0
    n_sym_kept = 0

    for disease, syms in dex_sym.items():
        if not is_valid_term(disease):
            continue
        clean = {s for s in syms if is_valid_term(s)}
        filtered_sym += len(syms) - len(clean)
        if not clean:
            continue
        n_sym_kept += 1
        for s in clean:
            sym_disease.append((s, disease))

    for disease, depts in dex_dep.items():
        if not is_valid_term(disease):
            continue
        for d in depts:
            targets = DEPARTMENT_MAP.get(d)
            if not targets:
                unmapped[d] += 1
                continue
            for tgt in targets:
                dis_dept.append((disease, tgt))

    print("    症状→疾病 关系数     : %d  (覆盖 %d 个疾病)" % (len(sym_disease), n_sym_kept))
    print("    疾病→科室 关系数     : %d" % len(dis_dept))
    print("    过滤掉的噪声症状数   : %d" % filtered_sym)
    if unmapped:
        print("    ⚠️ 未映射的数据集科室 : %s" % dict(unmapped))

    # 只保留「既有症状又有科室」的疾病，这样每条推荐链路都完整
    both = {d for d, _ in dis_dept} & {d for _, d in sym_disease}
    sym_disease = [(s, d) for s, d in sym_disease if d in both]
    dis_dept = [(d, t) for d, t in dis_dept if d in both]
    print("    可形成完整链路的疾病 : %d" % len(both))
    print("    最终 症状→疾病       : %d" % len(sym_disease))
    print("    最终 疾病→科室       : %d" % len(dis_dept))

    # ---------- 写文件 ----------
    print("\n[3/5] 写出产物到 %s" % OUT_DIR)

    def dump(name, header, rows):
        p = os.path.join(OUT_DIR, name)
        with io.open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        print("    %-26s %7d 行" % (name, len(rows)))

    dump("symptom_disease.csv", ["症状", "疾病"], sorted(set(sym_disease)))
    dump("disease_department.csv", ["疾病", "医院科室"], sorted(set(dis_dept)))
    dump("department_map.csv", ["数据集科室", "医院科室"],
         sorted((k, v) for k, vs in DEPARTMENT_MAP.items() for v in vs))

    # 备用关系（暂不启用，先落盘）
    check_rows = []
    for disease, checks in dex_check.items():
        if disease not in both:
            continue
        for c in checks:
            if is_valid_term(c):
                check_rows.append((disease, c))
    dump("disease_check.csv", ["疾病", "检查项目"], sorted(set(check_rows)))

    # ---------- 生成 Cypher 导入脚本 ----------
    print("\n[4/5] 生成 Neo4j 导入脚本 kg_import.cypher")
    _write_cypher(sym_disease, dis_dept, check_rows)

    # ---------- 质量报告 ----------
    if verify:
        print("\n[5/5] 质量报告")
        _write_report(sym_disease, dis_dept, dex_sym, dex_dep, unmapped, filtered_sym)

    if load_neo4j:
        print("\n[+] 导入 Neo4j ...")
        _load_neo4j(sym_disease, dis_dept)

    print("\n完成。产物目录: %s" % OUT_DIR)
    return 0


def _write_cypher(sym_disease, dis_dept, check_rows):
    """生成幂等导入脚本（MERGE，不 DELETE，可反复执行）"""
    path = os.path.join(OUT_DIR, "kg_import.cypher")
    L = []
    L.append("// 自动生成 —— 请勿手工编辑")
    L.append("// 数据来源: OpenCMKG ( https://github.com/RuiqingDing/OpenCMKG )")
    L.append("// 仅限学术研究使用")
    L.append("//")
    L.append("// 执行方式:")
    L.append("//   docker exec -i medical-neo4j cypher-shell -u neo4j -p <密码> < kg_import.cypher")
    L.append("//")
    L.append("// 说明: 全部使用 MERGE，可重复执行；本脚本不会删除已有数据")
    L.append("")

    # 约束（加速 MERGE，顺带保证唯一性）
    L.append("// ---- 唯一约束 ----")
    L.append("CREATE CONSTRAINT symptom_name IF NOT EXISTS FOR (n:Symptom) REQUIRE n.name IS UNIQUE;")
    L.append("CREATE CONSTRAINT disease_name IF NOT EXISTS FOR (n:Disease) REQUIRE n.name IS UNIQUE;")
    L.append("CREATE CONSTRAINT dept_name IF NOT EXISTS FOR (n:Department) REQUIRE n.name IS UNIQUE;")
    L.append("CREATE CONSTRAINT check_name IF NOT EXISTS FOR (n:CheckItem) REQUIRE n.name IS UNIQUE;")
    L.append("")

    # 用 UNWIND 批量导入，比逐条 MERGE 快几十倍
    L.append("// ---- 症状 → 疾病（表现为）----")
    L.append("UNWIND [")
    L.append(",\n".join('  ["%s","%s"]' % (s.replace('"', '\\"'), d.replace('"', '\\"'))
                       for s, d in sorted(set(sym_disease))))
    L.append("] AS row")
    L.append("MERGE (s:Symptom {name: row[0]})")
    L.append("MERGE (d:Disease {name: row[1]})")
    L.append("MERGE (d)-[:表现为]->(s);")
    L.append("")

    L.append("// ---- 疾病 → 科室（就诊于）----")
    L.append("UNWIND [")
    L.append(",\n".join('  ["%s","%s"]' % (d.replace('"', '\\"'), t.replace('"', '\\"'))
                       for d, t in sorted(set(dis_dept))))
    L.append("] AS row")
    L.append("MERGE (d:Disease {name: row[0]})")
    L.append("MERGE (dept:Department {name: row[1]})")
    L.append("MERGE (d)-[:就诊于]->(dept);")
    L.append("")

    if check_rows:
        L.append("// ---- 疾病 → 检查项目（需要检查）----")
        L.append("UNWIND [")
        L.append(",\n".join('  ["%s","%s"]' % (d.replace('"', '\\"'), c.replace('"', '\\"'))
                           for d, c in sorted(set(check_rows))))
        L.append("] AS row")
        L.append("MERGE (d:Disease {name: row[0]})")
        L.append("MERGE (c:CheckItem {name: row[1]})")
        L.append("MERGE (d)-[:需要检查]->(c);")
        L.append("")

    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L))
    size = os.path.getsize(path) / 1024.0 / 1024.0
    print("    %-26s %.1f MB" % ("kg_import.cypher", size))


def _write_report(sym_disease, dis_dept, dex_sym, dex_dep, unmapped, filtered_sym):
    """生成质量报告，方便写论文时引用数据"""
    dep_cnt = collections.Counter(t for _, t in dis_dept)
    sym_cnt = collections.Counter()
    for s, _ in sym_disease:
        sym_cnt[s] += 1
    multi = [x for x in sym_cnt.items() if x[1] >= 3]

    lines = []
    lines.append("# 知识图谱数据质量报告")
    lines.append("")
    lines.append("> 由 `knowledge/builders/build_kg.py --verify` 自动生成")
    lines.append("")
    lines.append("## 规模")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append("| 症状→疾病 关系 | %d |" % len(set(sym_disease)))
    lines.append("| 疾病→科室 关系 | %d |" % len(set(dis_dept)))
    lines.append("| 唯一症状数 | %d |" % len({s for s, _ in sym_disease}))
    lines.append("| 唯一疾病数 | %d |" % len({d for _, d in sym_disease}))
    lines.append("| 唯一科室数 | %d |" % len({t for _, t in dis_dept}))
    lines.append("| 过滤掉的噪声症状 | %d |" % filtered_sym)
    lines.append("")
    lines.append("## 原始数据")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append("| 原始有症状的疾病 | %d |" % len(dex_sym))
    lines.append("| 原始有科室的疾病 | %d |" % len(dex_dep))
    lines.append("")
    lines.append("## 科室分布（Top 20）")
    lines.append("")
    lines.append("| 医院科室 | 关联疾病数 |")
    lines.append("|---|---|")
    for d, c in dep_cnt.most_common(20):
        lines.append("| %s | %d |" % (d, c))
    lines.append("")
    lines.append("## 一症多病示例（可推荐多个科室）")
    lines.append("")
    lines.append("| 症状 | 关联疾病数 |")
    lines.append("|---|---|")
    for s, c in sorted(multi, key=lambda x: -x[1])[:15]:
        lines.append("| %s | %d |" % (s, c))
    lines.append("")
    if unmapped:
        lines.append("## ⚠️ 未映射的科室")
        lines.append("")
        lines.append("以下数据集科室在 `DEPARTMENT_MAP` 里没有配置，其关联疾病已被丢弃：")
        lines.append("")
        for d, c in unmapped.most_common():
            lines.append("- %s (%d 条)" % (d, c))
        lines.append("")
    lines.append("## 使用限制")
    lines.append("")
    lines.append("- 数据来源 OpenCMKG，**仅限学术研究，不得商用**")
    lines.append("- 科室映射表 `DEPARTMENT_MAP` 需要人工核对医院实际科室")
    lines.append("- 本图谱用于导诊参考，**不构成医疗诊断建议**")

    p = os.path.join(OUT_DIR, "report.md")
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    print("    %-26s 已生成" % "report.md")

    print("\n    --- 规模摘要 ---")
    print("    唯一症状 %d / 唯一疾病 %d / 唯一科室 %d" %
          (len({s for s, _ in sym_disease}), len({d for _, d in sym_disease}),
           len({t for _, t in dis_dept})))
    print("    科室分布 Top5: %s" % dep_cnt.most_common(5))


def _load_neo4j(sym_disease, dis_dept):
    """把生成好的 Cypher 灌进 Neo4j"""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("    跳过: 未安装 neo4j 驱动")
        return
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, ".env"))
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    pwd = os.getenv("NEO4J_PASSWORD", "")
    cypher_path = os.path.join(OUT_DIR, "kg_import.cypher")
    stmts = [s.strip() for s in io.open(cypher_path, encoding="utf-8").read().split(";") if s.strip()]
    stmts = [s for s in stmts if not s.lstrip().startswith("//")]
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pwd))
        drv.verify_connectivity()
        with drv.session() as ses:
            for i, s in enumerate(stmts, 1):
                ses.run(s)
                print("    语句 %d/%d 完成" % (i, len(stmts)))
        drv.close()
        print("    ✅ 导入完成")
    except Exception as e:
        print("    ❌ 导入失败: %s" % e)
        print("    （可以先只生成文件，Neo4j 起来后再跑 --load-neo4j）")


def main():
    ap = argparse.ArgumentParser(description="构建医学知识图谱数据")
    ap.add_argument("--verify", action="store_true", help="生成质量报告")
    ap.add_argument("--load-neo4j", action="store_true", help="生成后导入 Neo4j")
    a = ap.parse_args()
    sys.exit(build(verify=a.verify, load_neo4j=a.load_neo4j))


if __name__ == "__main__":
    main()
