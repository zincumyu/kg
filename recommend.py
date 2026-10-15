# -*- coding: utf-8 -*-
"""
图遍历 + 加权投票 → 科室推荐
============================

把「一组症状节点」变成「推荐科室 + 置信度 + 依据」。

链路
----
    起点节点（症状 / 疾病）
        │
        ├─ 起点是「症状」：症状 ←表现为— 疾病 —就诊于→ 科室
        │
        └─ 起点是「疾病」：疾病 —就诊于→ 科室（只走直接边）
        │
        ▼
    加权投票 → 按票数排序
        │
        ▼
    [{科室, 票数, 置信度, 依据}, ...]

两个实测踩过的坑
----------------
1. **疾病起点不要做症状扩散**
   曾试过「疾病 → 它的症状 → 其他疾病 → 科室」来扩大召回，结果有害：
   腰腿痛直接就诊于「外科普诊」只有 1 票，扩散后污染成 191 票，
   把「外科普诊」顶到第一名，反而压掉了更合理的「骨科专科」。
   → 医疗场景精度优先于召回，疾病起点只走直接边。

2. **置信度是相对值**
   confidence = 该科室票数 / 总票数，范围 0~1。
   它是「相对占比」，**不是准确率**，与申请书里的阈值 0.85 量纲不同，不能混用。
"""

import logging
import collections

import db

logger = logging.getLogger(__name__)

def _env_float(key, default):
    """从 .env / 环境变量读浮点配置（读不到就用默认值）"""
    try:
        import os
        from paths import load_env
        load_env()
        return float(os.getenv(key) or default)
    except Exception:
        return float(default)


# 置信度阈值：低于此值认为「推荐不可靠」，上层应回答「不确定」
# 可在 tests/kg/.env 里用 KG_MIN_CONFIDENCE 调整
# 依据实测数据标定：
#   牙疼 → 口腔内科 0.71
#   腿疼 → 骨科专科 0.33
#   拉肚子 → 消化专科 0.19
#   上不来气 → 呼吸内科 0.16
#   无关文本 → 0（无结果）
MIN_CONFIDENCE = _env_float("KG_MIN_CONFIDENCE", 0.15)

# 单个节点的邻居数量上限（防止超级节点拖慢查询）
NEIGHBOR_LIMIT = 400

# ----------------------------------------------------------------------
# 儿科科室识别
# ----------------------------------------------------------------------
# 为什么要识别：实测患者说「上不来气」时，「儿科内科」得 102 票压过「呼吸内科」100 票，
# 因为数据集里小儿呼吸道疾病的条目很多。成年人被推荐儿科是明显错误。
#
# ⚠️ 不能简单用「儿」字匹配 —— 「反复种植失败专科门诊(试管婴儿)」含「儿」但属于生殖科。
PEDIATRIC_PATTERNS = ("儿科", "小儿", "儿童", "新生儿", "高危儿", "早产儿", "婴幼儿", "少儿")
NOT_PEDIATRIC_PATTERNS = ("试管婴儿", "胎儿", "产前", "孕")

# 儿科就诊年龄界限（周岁），可在 tests/kg/.env 里用 KG_PEDIATRIC_MAX_AGE 调整
PEDIATRIC_MAX_AGE = _env_float("KG_PEDIATRIC_MAX_AGE", 14)


def is_pediatric_department(name):
    """判断是不是儿科类科室"""
    if not name:
        return False
    if any(k in name for k in NOT_PEDIATRIC_PATTERNS):
        return False
    return any(k in name for k in PEDIATRIC_PATTERNS)


class _NameCache:
    """node_key → (name, type) 的小缓存，避免同一批遍历里重复查库"""

    def __init__(self):
        self._c = {}

    def get(self, key):
        if key not in self._c:
            row = db.query_one("SELECT name, node_type FROM kg_node WHERE node_key=%s", (key,))
            self._c[key] = (row["name"], row["node_type"]) if row else (key, "")
        return self._c[key]


def traverse(node_keys, top_n=3, min_confidence=None, age=None):
    """
    从一组起点节点遍历到科室，按票数排序。

    参数
    ----
    node_keys : 起点节点键列表（来自 NodeSelector）
    top_n     : 返回前几名
    min_confidence : 置信度阈值，None 用模块默认值
    age       : 患者年龄（周岁）。用于过滤儿科科室，规则：
                  age < 14   → 保留全部（儿童也可能去通用科室）
                  age >= 14  → 排除儿科类科室
                  age 未知   → **也排除儿科类科室**
                最后一条是刻意的策略：综合医院里成年患者占多数，
                给成年人推荐儿科，比不给儿童推荐儿科，是更严重的错误。

    返回
    ----
    {
      "departments": [
          {"科室": "骨科专科", "科室键": "department:骨科专科",
           "票数": 5, "置信度": 0.33, "依据": ["关节退行性变", ...]},
          ...
      ],
      "total_votes": 15,
      "enough": True,            # 置信度是否达标
      "confidence": 0.33,        # 第一名的置信度
      "filtered_pediatric": [...]  # 被年龄规则过滤掉的儿科科室（如有）
    }
    """
    node_keys = [k for k in (node_keys or []) if k]
    if not node_keys:
        return {"departments": [], "total_votes": 0,
                "enough": False, "confidence": 0.0}

    cache = _NameCache()
    score = collections.Counter()
    evidence = collections.defaultdict(list)
    seen_disease = set()

    # 年龄过滤策略：只有明确知道是儿童时才保留儿科科室
    keep_pediatric = (age is not None and age < PEDIATRIC_MAX_AGE)
    skipped_pediatric = set()

    def add_departments_of(disease_key):
        """把某个疾病的所有科室加票"""
        if disease_key in seen_disease:
            return
        seen_disease.add(disease_key)
        rows = db.query(
            "SELECT dst_key FROM kg_edge WHERE src_key=%s AND relation='就诊于' LIMIT %s",
            (disease_key, NEIGHBOR_LIMIT))
        for r in rows:
            dept_key = r["dst_key"]
            dname, _ = cache.get(dept_key)
            if not keep_pediatric and is_pediatric_department(dname):
                skipped_pediatric.add(dname)
                continue
            score[dept_key] += 1
            if len(evidence[dept_key]) < 6:
                evidence[dept_key].append(disease_key)

    for k in node_keys:
        _, ntype = cache.get(k)

        if ntype == "symptom":
            # 症状 ←表现为— 疾病（表现为 的方向是 疾病→症状，所以查 dst_key）
            rows = db.query(
                "SELECT src_key FROM kg_edge WHERE dst_key=%s AND relation='表现为' LIMIT %s",
                (k, NEIGHBOR_LIMIT))
            for r in rows:
                add_departments_of(r["src_key"])

        elif ntype == "disease":
            # 疾病直接就诊于科室。⚠️ 不做症状扩散（见文件头说明）
            add_departments_of(k)

        elif ntype == "department":
            # 起点本来就是科室，直接计一票
            score[k] += 1
            if len(evidence[k]) < 6:
                evidence[k].append(k)

    total = sum(score.values())
    if not total:
        return {"departments": [], "total_votes": 0,
                "enough": False, "confidence": 0.0}

    thr = MIN_CONFIDENCE if min_confidence is None else min_confidence

    out = []
    for dept_key, votes in score.most_common(top_n):
        dname, _ = cache.get(dept_key)
        out.append({
            "科室": dname,
            "科室键": dept_key,
            "票数": votes,
            "置信度": round(votes / float(total), 4),
            "依据": [cache.get(x)[0] for x in evidence[dept_key]],
        })

    top_conf = out[0]["置信度"] if out else 0.0
    res = {
        "departments": out,
        "total_votes": total,
        "confidence": top_conf,
        "enough": top_conf >= thr,
    }
    if skipped_pediatric:
        res["filtered_pediatric"] = sorted(skipped_pediatric)[:8]
    return res


def reason_text(result, max_evidence=3):
    """把遍历结果转成一句可读的推荐理由，便于拼进回答"""
    deps = result.get("departments") or []
    if not deps:
        return ""
    top = deps[0]
    ev = "、".join(top["依据"][:max_evidence])
    txt = "建议前往【%s】就诊" % top["科室"]
    if ev:
        txt += "（依据：%s）" % ev
    if len(deps) > 1:
        txt += "。其他可能科室：%s" % "、".join(d["科室"] for d in deps[1:])
    return txt


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    from store import get_store

    s = get_store()
    for text in ["腿疼", "上不来气", "拉肚子", "牙疼", "今天天气真好"]:
        keys = [n["node_key"] for n in s.search_nodes(text)["nodes"]]
        r = traverse(keys)
        print("【%s】 节点=%s" % (text, keys[:3]))
        if r["departments"]:
            for d in r["departments"]:
                print("    %-12s 票 %3d  置信度 %.2f  依据: %s"
                      % (d["科室"], d["票数"], d["置信度"], "、".join(d["依据"][:2])))
            print("    达标: %s" % r["enough"])
            if r.get("filtered_pediatric"):
                print("    已过滤儿科: %s" % r["filtered_pediatric"])
        else:
            print("    (无推荐)")
        print()
