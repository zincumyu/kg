# -*- coding: utf-8 -*-
"""
知识图谱节点导入器（ETL）
=========================

把「公开数据集 + 医院真实数据」转换成统一的节点/边/别名，写入 MySQL。

用法（在 LLM_server 目录下执行）：

    python knowledge/builders/import_kg_nodes.py              # 全量重建（默认）
    python knowledge/builders/import_kg_nodes.py --stats      # 只打印统计，不导入
    python knowledge/builders/import_kg_nodes.py --no-clear   # 增量导入（不清表）

数据流：

    data/opencmkg/triples.txt ─┐
                               ├─► kg_node / kg_edge / kg_alias (MySQL)
    data/3.排班表数据.xlsx      ─┤
    data/科室检查项目1.xlsx      ─┘

设计说明见 dev_h 的方案文档。核心原则：
    1. 所有实体统一进 kg_node，用 node_key = "<类型>:<名称>" 做稳定标识
    2. 科室节点统一用「医院真实挂号科室名」，保证推荐结果患者能挂到号
    3. 别名主要来自数据集自带的 SameAs 关系（17,956 条），
       再叠加 data/kg/symptom_synonyms.csv 里人工维护的补充同义词
       （手工表优先级更高，见 run() 的「④.5 手工同义词」）
"""

import os
import sys
import io
import csv
import json
import time
import argparse
import re
import collections

# ---- 数据访问层（纯 pymysql，不依赖 Django）----
BASE = os.path.dirname(os.path.abspath(__file__))          # tests/kg/builders/
PROJ = os.path.dirname(os.path.dirname(os.path.dirname(BASE)))   # 项目根
sys.path.insert(0, os.path.dirname(BASE))                  # tests/kg/
import db                                                  # noqa: E402

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

# ---- 路径 ----
KG_DIR = os.path.join(PROJ, "data", "kg")
TRIPLES = os.path.join(PROJ, "data", "opencmkg", "triples.txt")
SCHEDULE_XLSX = os.path.join(PROJ, "data", "3.排班表数据.xlsx")
CHECK_XLSX = os.path.join(PROJ, "data", "科室检查项目1.xlsx")
F_SYMPTOM_DISEASE = os.path.join(KG_DIR, "symptom_disease.csv")
F_DISEASE_DEPT = os.path.join(KG_DIR, "disease_department.csv")
F_SYNONYMS = os.path.join(KG_DIR, "symptom_synonyms.csv")

SRC = "opencmkg"
SRC_H = "hospital"
SRC_MANUAL = "manual"      # 人工维护的同义词（data/kg/symptom_synonyms.csv）

# 批量写入的批大小
BATCH = 2000

# ----------------------------------------------------------------------
# 别名过滤规则
# ----------------------------------------------------------------------
# 数据集自带的 SameAs 里有少量噪声，需要在导入时剔除。规则基于实测数据设计：
#
# 规则 1｜真否定：否定词 + 疼痛类动词，且位于**结尾**。
#   例：腰没有疼 / 肚子不疼 / 尿不疼  这些表达意思是「不痛」，不该映射到「腰痛」。
#   ⚠️ 必须限定在结尾，否则会误删：
#     「尿不痛快」= 排尿不畅（真症状），「上不来气」= 呼吸困难（真症状），
#     「三天不拉屎」= 便秘（真症状）—— 这些都不能删。
#
# 规则 2｜过度泛化：一个别名指向太多节点时，它不携带有效信息。
#   例：「痛」→ 35 个节点、「疼」→ 35 个、「疼痛」→ 27 个。
#   这类词单独出现时无法定位具体概念，保留只会污染投票。
#
# 规则 3｜歧义别名（指向 2~5 个节点）**保留**。
#   例：「腿疼」同时指向「腰痛」「下肢疼痛」「小腿疼痛」「腰腿痛」。
#   这在医学上确实有歧义，交给下游的加权投票机制处理，不在此处武断取舍。
NEGATION_TAIL = re.compile(r"(没有|没|不|无|未)(疼|痛|痒|肿)$")
MAX_ALIAS_FANOUT = 5

# ----------------------------------------------------------------------
# 死胡同簇的桥接参数
# ----------------------------------------------------------------------
# 数据集里有一批「只参与 SameAs、没有任何疾病边」的簇标签（如 发热 / 睡眠不佳 / 胃胀）。
# 它们本身是合理的医学概念，但直接用会走进死胡同（推不出科室）。
# 解决办法：看两个簇共享多少个别名，共享多说明语义接近，可据此桥接。
#   簇「疼痛」 vs 簇「腹痛」   共享 20 条 → 桥接
#   簇「鼻涕」 vs 簇「乳头溢液」 共享  1 条 → 巧合，丢弃
# 判据是「占比」而不是「绝对数」——这一点很关键，实测对比：
#   睡眠不佳 → 入睡困难  占 77%  ✅ 可信
#   发热     → 高热      占 40%  ✅ 可信
#   胃胀     → 腹胀      占 38%  ✅ 可信
#   下肢疼痛 → 腰痛      占  6%  ❌ 噪声（各候选都 ≤6%，是巧合）
# 只用绝对数会导致「下肢疼痛→腰痛」这种错误桥接，
# 进而把「腿疼」污染成指向 牙痛/尿痛 等无关节点。
BRIDGE_MIN_SHARED = 3      # 绝对门槛：至少共享 3 个别名
BRIDGE_MIN_SHARE = 0.30    # 占比门槛：Top 目标须占全部共享数的 30% 以上
BRIDGE_MAX_TARGETS = 3     # 最多桥接 3 个目标

# ----------------------------------------------------------------------
# 实体名合法性（与 build_kg.py 保持一致）
# ----------------------------------------------------------------------
# 用于判断 SameAs 的左端术语是否可作为规范节点名。
#
# 实测数据：SameAs 共 1,203 个左端术语，其中 730 个不在原有节点集中
# （因为症状节点原先只来自 disease_has_symptom 关系，不包含仅出现在别名里的术语），
# 这 730 个术语覆盖了 8,626 条别名。若不补成节点，这些别名会被全部丢弃。
# 典型漏掉的术语：发热、睡眠不佳、月经提前、咽痒、胃胀、大便性状改变……
_ILLEGAL = re.compile(r"[0-9A-Za-z\s,;:!?。，；：！？\"'()（）\[\]{}<>/\|@#$%^&*+=~`]")


def is_valid_term(s):
    """判断实体名是否为干净的中文医学词"""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 2 or len(s) > 30:
        return False
    if _ILLEGAL.search(s):
        return False
    return not s.isdigit()



# ======================================================================
# 工具
# ======================================================================
def key(node_type, name):
    """生成节点唯一键"""
    return "%s:%s" % (node_type, str(name).strip())


def read_csv(path):
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def bulk(rows, label, batch=BATCH):
    """分批写入，返回写入条数"""
    n = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        n += len(chunk)
        yield chunk, n


# ======================================================================
# 各数据源的解析
# ======================================================================
def parse_symptom_disease():
    """
    症状 → 疾病（来自 data/kg/symptom_disease.csv，已清洗）
    关系方向：疾病 -[表现为]-> 症状
    返回 (nodes, edges)
    """
    nodes, edges = [], []
    seen = set()
    for r in read_csv(F_SYMPTOM_DISEASE):
        s = (r.get("症状") or "").strip()
        d = (r.get("疾病") or "").strip()
        if not s or not d:
            continue
        sk, dk = key("symptom", s), key("disease", d)
        if sk not in seen:
            seen.add(sk)
            nodes.append({"node_key": sk, "node_type": "symptom", "name": s,
                              "source": SRC, "extra": None})
        if dk not in seen:
            seen.add(dk)
            nodes.append({"node_key": dk, "node_type": "disease", "name": d,
                              "source": SRC, "extra": None})
        edges.append((dk, sk, "表现为", SRC))
    return nodes, edges


def parse_disease_department():
    """
    疾病 → 科室（来自 data/kg/disease_department.csv，已映射到医院真实科室名）
    关系方向：疾病 -[就诊于]-> 科室
    """
    nodes, edges = [], []
    seen = set()
    for r in read_csv(F_DISEASE_DEPT):
        d = (r.get("疾病") or "").strip()
        dept = (r.get("医院科室") or "").strip()
        if not d or not dept:
            continue
        dk, pk = key("disease", d), key("department", dept)
        if dk not in seen:
            seen.add(dk)
            nodes.append({"node_key": dk, "node_type": "disease", "name": d,
                              "source": SRC, "extra": None})
        if pk not in seen:
            seen.add(pk)
            nodes.append({"node_key": pk, "node_type": "department", "name": dept,
                              "source": SRC, "extra": {"bookable": True}})
        edges.append((dk, pk, "就诊于", SRC))
    return nodes, edges


def parse_synonyms():
    """
    手工维护的口语同义词（data/kg/symptom_synonyms.csv）

    这是**唯一需要人工维护的别名来源**。OpenCMKG 的 SameAs 覆盖不到很多
    日常说法，实测「膝盖疼 / 耳朵响 / 有痰 / 脚肿 / 胃胀 / 手抖 / 崴脚 /
    胳膊疼 / 手指麻」等 27 个常用词在自动管道里查不到，患者说出来只能落到
    LLM 兜底（约 10 秒）或直接答不上来。

    返回 [(口语表达, 规范术语), ...]，规范术语必须是图谱里已存在的节点名。

    注意：本函数**不做**「别名自身是否已是节点名」的判断，那个判断需要
    等节点收集完才能做，放在 run() 里（见 inject_manual_aliases）。
    """
    if not os.path.exists(F_SYNONYMS):
        return []
    out = []
    for r in read_csv(F_SYNONYMS):
        al = (r.get("口语表达") or "").strip()
        tgt = (r.get("规范术语") or "").strip()
        if al and tgt:
            out.append((al, tgt))
    return out


def parse_triples_extra():
    """
    从 triples.txt 补充三类关系：
      disease_need_check     疾病 -[需要检查]-> 检查项
      disease_acompany_disease 疾病 -[并发症]-> 疾病
      SameAs                 别名（口语 → 规范术语）
    返回 (nodes, edges, aliases)
    """
    nodes, edges, aliases = [], [], []
    seen = set()
    n_raw = 0

    with io.open(TRIPLES, encoding="utf-8", errors="ignore") as f:
        for line in f:
            parts = [p.strip() for p in line.rstrip("\n").split(",")]
            if len(parts) != 3:
                continue
            h, r, t = parts
            if not h or not t:
                continue
            n_raw += 1

            if r == "disease_need_check":
                hk, tk = key("disease", h), key("check_item", t)
                if hk not in seen:
                    seen.add(hk)
                    nodes.append({"node_key": hk, "node_type": "disease", "name": h,
                                      "source": SRC, "extra": None})
                if tk not in seen:
                    seen.add(tk)
                    nodes.append({"node_key": tk, "node_type": "check_item", "name": t,
                                      "source": SRC, "extra": None})
                edges.append((hk, tk, "需要检查", SRC))

            elif r == "disease_acompany_disease":
                hk, tk = key("disease", h), key("disease", t)
                for k_, nm in ((hk, h), (tk, t)):
                    if k_ not in seen:
                        seen.add(k_)
                        nodes.append({"node_key": k_, "node_type": "disease", "name": nm,
                                          "source": SRC, "extra": None})
                edges.append((hk, tk, "并发症", SRC))

            elif r == "SameAs":
                # 方向：左端为规范术语，右端为口语别名（已实测确认）
                aliases.append((t, h))

    return nodes, edges, aliases, n_raw


def parse_hospital_departments_and_doctors():
    """
    从排班表导入医院真实科室 + 医生
      Department (bookable=True)
      Doctor
      Doctor -[所属科室]-> Department
    """
    try:
        import pandas as pd
    except ImportError:
        print("  ⚠️ 未安装 pandas，跳过医院数据")
        return [], []

    nodes, edges = [], []
    seen = set()
    df = pd.read_excel(SCHEDULE_XLSX)

    for _, row in df.iterrows():
        dept = str(row.get("ejksmc", "")).strip()
        doctor = str(row.get("ysmc", "")).strip()
        yj = str(row.get("yjksmc", "")).strip()
        if dept in ("", "nan"):
            continue

        dk = key("department", dept)
        if dk not in seen:
            seen.add(dk)
            nodes.append({"node_key": dk, "node_type": "department", "name": dept,
                                  "source": SRC_H,
                                  "extra": {"bookable": True, "parent": yj if yj != "nan" else None}})

        if doctor and doctor != "nan":
            doc = doctor.replace(" ", "")          # 姓名里常有全角空格
            doctor_key = key("doctor", doc)
            if doctor_key not in seen:
                seen.add(doctor_key)
                nodes.append({"node_key": doctor_key, "node_type": "doctor",
                                  "name": doc, "source": SRC_H,
                                  "extra": {"dept": dept}})
                edges.append((doctor_key, dk, "所属科室", SRC_H))

    return nodes, edges


def parse_hospital_checks():
    """
    从「科室检查项目1.xlsx」导入检查项（含物理位置与注意事项）
      CheckItem (extra.location / extra.prep)
    """
    try:
        import pandas as pd
    except ImportError:
        return [], []

    if not os.path.exists(CHECK_XLSX):
        return [], []

    nodes, edges = [], []
    seen = set()
    df = pd.read_excel(CHECK_XLSX)

    for _, row in df.iterrows():
        name = str(row.get("检查项目名称", "")).strip().rstrip("_")
        if not name or name == "nan":
            continue
        loc = str(row.get("问诊说明", "")).strip()
        prep = str(row.get("注意事项", "")).strip()
        cat = str(row.get("检测分类名称", "")).strip()

        ck = key("check_item", name)
        extra = {}
        if loc and loc != "nan":
            extra["location"] = loc
        if prep and prep != "nan":
            extra["prep"] = prep
        if cat and cat != "nan":
            extra["category"] = cat

        if ck not in seen:
            seen.add(ck)
            nodes.append({"node_key": ck, "node_type": "check_item", "name": name,
                                  "source": SRC_H, "extra": extra or None})

        # 检查项归属科室（用分类名做弱关联，若医院没有该科室则跳过）
        if cat and cat != "nan":
            pk = key("department", cat)
            if pk not in seen:
                # 只有当该科室确实存在时才建边（避免造出不可挂号的科室）
                pass

    return nodes, edges


# ======================================================================
# 主流程
# ======================================================================
def dedup_nodes(all_nodes):
    """按 node_key 去重，后者覆盖前者的 extra（医院数据优先）"""
    m = {}
    for n in all_nodes:
        k = n["node_key"]
        if k in m:
            # 合并 extra：非空的覆盖空的
            merged = dict(m[k].get("extra") or {})
            merged.update(n.get("extra") or {})
            if merged:
                m[k]["extra"] = merged
            # hospital 来源优先
            if n.get("source") == SRC_H:
                m[k]["source"] = SRC_H
        else:
            m[k] = n
    return list(m.values())


def dedup_edges(all_edges):
    """按 (src, dst, relation) 去重（元素是 (src,dst,relation,source) 元组）"""
    seen = set()
    out = []
    for e in all_edges:
        t = (e[0], e[1], e[2])
        if t in seen:
            continue
        seen.add(t)
        out.append(e)
    return out


def run(clear=True, stats_only=False):
    t0 = time.time()
    print("=" * 70)
    print("  知识图谱节点导入")
    print("=" * 70)

    for p in (F_SYMPTOM_DISEASE, F_DISEASE_DEPT, TRIPLES):
        if not os.path.exists(p):
            print("❌ 缺少数据文件: %s" % p)
            if "triples" in p:
                print("   请先按 docs/ 说明下载 OpenCMKG 到 data/opencmkg/")
            else:
                print("   请先运行: python knowledge/builders/build_kg.py")
            return 1

    # ---------- 解析 ----------
    print("\n[1/4] 解析数据源 ...")

    n1, e1 = parse_symptom_disease()
    print("  症状-疾病        : %6d 节点 %6d 边" % (len(n1), len(e1)))

    n2, e2 = parse_disease_department()
    print("  疾病-科室        : %6d 节点 %6d 边" % (len(n2), len(e2)))

    n3, e3, aliases, n_raw = parse_triples_extra()
    print("  检查/并发症/别名 : %6d 节点 %6d 边 %6d 别名(原始)" % (len(n3), len(e3), len(aliases)))

    n4, e4 = parse_hospital_departments_and_doctors()
    print("  医院科室-医生    : %6d 节点 %6d 边" % (len(n4), len(e4)))

    n5, e5 = parse_hospital_checks()
    print("  医院检查项       : %6d 节点" % len(n5))

    # ---------- 合并去重 ----------
    print("\n[2/4] 合并去重 ...")
    nodes = dedup_nodes(n1 + n2 + n3 + n4 + n5)
    edges = dedup_edges(e1 + e2 + e3 + e4 + e5)
    by_type = collections.Counter(n["node_type"] for n in nodes)
    print("  节点合计 %d，边合计 %d" % (len(nodes), len(edges)))
    for t, c in by_type.most_common():
        print("    %-12s %6d" % (t, c))

    # ---------- 别名（第 3 层：文本 → 节点 的数据基础）----------
    print("\n[3/4] 处理别名 ...")

    # ① 构建「簇」结构。
    #    数据集用 SameAs 把大量口语表达归并到一个规范术语下，形成一个个"簇"。
    #    例如 簇「下肢疼痛」包含：腿疼 / 右腿痛 / 整条腿都疼 / 腿动也疼 …（60 条）
    cluster_alias = collections.defaultdict(set)    # 簇名 -> {口语}
    alias_cluster = collections.defaultdict(set)    # 口语 -> {所属簇}
    for al, canon in aliases:
        if not al or not canon:
            continue
        cluster_alias[canon].add(al)
        alias_cluster[al].add(canon)

    # ② 区分「真实簇」与「死胡同簇」。
    #    真实簇：名字本身就是图谱节点（有疾病边，能推出科室）
    #    死胡同簇：只参与 SameAs，没有任何疾病边（如「发热」「睡眠不佳」），
    #              直接使用会导致推荐链路走不通，必须桥接到真实节点
    real_names = {n["name"] for n in nodes}
    dead = {k: v for k, v in cluster_alias.items() if k not in real_names}
    print("  别名簇 %d 个：真实簇 %d / 死胡同簇 %d"
          % (len(cluster_alias), len(cluster_alias) - len(dead), len(dead)))

    # ③ 为死胡同簇寻找桥接目标。
    #    判据：两个簇共享的别名数。共享越多，说明语义越接近。
    #    例：簇「疼痛」与簇「腹痛」共享 20 个别名 → 可桥接
    #        簇「鼻涕」与簇「乳头溢液」共享 1 个     → 视为巧合，丢弃
    bridges = {}
    for k, als in dead.items():
        owners = collections.Counter()
        for a in als:
            for o in alias_cluster.get(a, ()):
                if o in real_names and o != k:
                    owners[o] += 1
        total = sum(owners.values())
        if not total:
            continue
        # 同时满足「绝对数」与「占比」两个门槛才认可
        good = [o for o, c in owners.most_common(BRIDGE_MAX_TARGETS)
                if c >= BRIDGE_MIN_SHARED and (c / float(total)) >= BRIDGE_MIN_SHARE]
        if good:
            bridges[k] = good
    n_bridged_aliases = sum(len(dead[k]) for k in bridges)
    print("  桥接成功 %d 个死胡同簇（覆盖 %d 条别名），未桥接 %d 个"
          % (len(bridges), n_bridged_aliases, len(dead) - len(bridges)))

    # ④ 解析每个口语表达最终指向哪些节点
    # 名字 → 节点键。
    #
    # ⚠️ 关键：647 个名字同时存在「症状」和「疾病」两种节点（如 腰痛 / 胸痛 / 血尿）。
    #    如果简单地取第一个，别名可能挂到「没有任何边的死节点」上，
    #    导致后续图遍历直接断掉（实测：腿疼 → disease:腰痛，该节点出边为 0）。
    #
    #    解决：同名时选「边更多」的那个节点 —— 边多说明它在图里真的被使用。
    #      腰痛  : symptom(14 条边) vs disease(2 条)  → 选 symptom ✅
    #      腰腿痛: disease(21 条边) vs symptom(不存在) → 选 disease ✅
    _edge_cnt = collections.Counter()
    for _e in edges:
        _edge_cnt[_e[0]] += 1
        _edge_cnt[_e[1]] += 1

    name_to_key = {}
    for n in nodes:
        cur = name_to_key.get(n["name"])
        if cur is None or _edge_cnt[n["node_key"]] > _edge_cnt[cur]:
            name_to_key[n["name"]] = n["node_key"]

    _dead = [n for n in nodes if _edge_cnt[n["node_key"]] == 0]
    if _dead:
        print("  提示：%d 个节点没有任何边（孤立节点）" % len(_dead))
    alias_targets = collections.defaultdict(set)
    unresolved = 0
    for al, canon in aliases:
        if not al or not canon:
            continue
        targets = [canon] if canon in real_names else bridges.get(canon, [])
        if not targets:
            unresolved += 1
            continue
        for t in targets:
            nk = name_to_key.get(t)
            if nk and al != t:
                alias_targets[al].add(nk)

    # ④.5 手工同义词（优先级最高，覆盖自动管道的结果）
    #
    # 为什么单独做：手工表的「规范术语」是医学审核人员指定的，比 SameAs + 桥接
    # 推断出来的更可信，所以同名时直接覆盖，而不是合并。
    #
    # 为什么要跳过「别名本身已是节点名」：
    #   store.search_nodes 的优先级是  alias(0) > name_exact(1) > name_partial(2)
    #   即别名匹配**高于**节点名匹配。若给「心慌」加别名「→心悸」，那么患者说
    #   「心慌」时返回的是「心悸」而不是节点「心慌」—— 这属于倒退。
    #   实测有 16 条属于这种情况（胸口痛/心慌/关节痛/流鼻涕/喉咙痛/腰酸/打嗝/
    #   烧心/手脚发麻/抽筋/打呼噜/咳血/流鼻血/消瘦/皮肤发黄/落枕），
    #   它们本来就靠 name_exact 命中，不需要也不应该加别名。
    manual_pairs = parse_synonyms()
    # CSV 格式是 1:1（每个口语表达一行，见 test_synonyms_unique 的强制校验）。
    # 这里仍按「一口语 → 多目标」聚合：万一有人写了重复行，是合并而不是后者悄悄
    # 覆盖前者；正常数据下与直接赋值等价。
    manual_map = {}
    for al, canon in manual_pairs:
        manual_map.setdefault(al, []).append(canon)

    manual_add, manual_shadow, manual_dead, manual_dup = [], [], [], []
    manual_conflict = []                  # 手工表与自动管道指向不同节点
    for al, canons in manual_map.items():
        if al in real_names:
            manual_shadow.append(al)
            continue
        nks = set()
        for canon in canons:
            nk = name_to_key.get(canon)
            if nk:
                nks.add(nk)
            else:
                manual_dead.append((al, canon))
        if not nks:
            continue
        old = set(alias_targets.get(al) or ())
        if nks <= old:                    # 自动管道已覆盖手工的全部目标 → 无需改动
            manual_dup.append(al)
            continue
        if old:
            manual_conflict.append((al, sorted(old), sorted(nks)))
        alias_targets[al] = nks           # ← 覆盖，手工表权威
        manual_add.append(al)
    manual_set = set(manual_add)          # 供第⑤步标记 source='manual'

    if manual_pairs:
        print("  手工同义词 %d 条（%d 个口语表达）→ 新增 %d，"
              "跳过(别名本身是节点名) %d，已有 %d，死链 %d"
              % (len(manual_pairs), len(manual_map), len(manual_add),
                 len(manual_shadow), len(manual_dup), len(manual_dead)))
        if manual_dead:
            print("    ⚠️  死链（规范术语在图谱中不存在，请修正 CSV）: %s"
                  % manual_dead[:5])
        if manual_conflict:
            print("    ⚠️  与自动管道冲突 %d 条（手工表覆盖，原指向已丢弃）:" % len(manual_conflict))
            for al, olds, news in manual_conflict[:8]:
                print("        %-8s 自动→%s  手工→%s"
                      % (al, "、".join(olds), "、".join(news)))

    # ⑤ 过滤规则（见文件头 ASCII 常量处的详细说明）
    alias_rows, dropped_neg, dropped_fanout, dropped_short = [], [], [], []
    for al, keys in alias_targets.items():
        # 规则 0：单字别名过滤。
        # 如「咳」「痰」「血」「痛」—— 它们本身含义太泛，且在将来做子串匹配时
        # 会在无关词里误命中（如「咯」出现在别的词中）。与 is_valid_term 的
        # 「长度 >= 2」规则保持一致。
        if len(al.strip()) < 2:
            dropped_short.append(al)
            continue
        if NEGATION_TAIL.search(al):
            dropped_neg.append(al)
            continue
        if len(keys) > MAX_ALIAS_FANOUT:
            dropped_fanout.append((al, len(keys)))
            continue
        for nk in keys:
            alias_rows.append((al, nk, SRC_MANUAL if al in manual_set else SRC))

    print("  可用别名 %d 条（去重后覆盖 %d 个口语表达）"
          % (len(alias_rows), len({r[0] for r in alias_rows})))
    print("  丢弃：无有效桥接      %d 条" % unresolved)
    print("  丢弃：单字别名        %d 条  %s" % (len(dropped_short), dropped_short[:6]))
    print("  丢弃：真否定表达      %d 条  %s" % (len(dropped_neg), dropped_neg[:4]))


    print("  丢弃：过度泛化(>%d节点) %d 条  %s"
          % (MAX_ALIAS_FANOUT, len(dropped_fanout), [x[0] for x in dropped_fanout[:4]]))

    if stats_only:
        print("\n（--stats 模式，未写入数据库）")
        return 0

    # ---------- 写入 ----------
    print("\n[4/4] 写入 MySQL ...")

    ok, desc = db.health()
    if not ok:
        print("  ❌ 数据库不可用: %s" % desc)
        print("     请确认 MySQL 容器在运行（docker ps ^| findstr medical-mysql）")
        return 1
    print("  目标: %s" % desc)

    if clear:
        print("  清空旧数据 ...")
        # 顺序：先删边和别名（它们引用节点），再删节点
        db.execute("DELETE FROM kg_edge")
        db.execute("DELETE FROM kg_alias")
        db.execute("DELETE FROM kg_node")

    # 节点：dict 列表 → 元组列表
    node_rows = [(n["node_key"], n["node_type"], n["name"],
                  json.dumps(n.get("extra"), ensure_ascii=False) if n.get("extra") else None,
                  n["source"]) for n in nodes]
    for chunk, _ in bulk(node_rows, "节点"):
        db.insert_many("kg_node", ["node_key", "node_type", "name", "extra", "source"], chunk)
    print("  节点  写入 %6d" % db.query_one("SELECT COUNT(*) AS c FROM kg_node")["c"])

    for chunk, _ in bulk(edges, "边"):
        db.insert_many("kg_edge", ["src_key", "dst_key", "relation", "source"], chunk)
    print("  边    写入 %6d" % db.query_one("SELECT COUNT(*) AS c FROM kg_edge")["c"])

    for chunk, _ in bulk(alias_rows, "别名"):
        db.insert_many("kg_alias", ["alias", "node_key", "source"], chunk)
    print("  别名  写入 %6d" % db.query_one("SELECT COUNT(*) AS c FROM kg_alias")["c"])

    print("\n完成，耗时 %.1f 秒" % (time.time() - t0))
    print("=" * 70)
    return 0


def main():
    ap = argparse.ArgumentParser(description="知识图谱节点导入器")
    ap.add_argument("--stats", action="store_true", help="只统计不写入")
    ap.add_argument("--no-clear", action="store_true", help="增量导入，不清空旧数据")
    a = ap.parse_args()
    sys.exit(run(clear=not a.no_clear, stats_only=a.stats))


if __name__ == "__main__":
    main()
