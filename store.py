# -*- coding: utf-8 -*-
"""
知识图谱节点检索工具（只读）
============================

给 LLM 提供 1 个工具，让它能「查图谱」但**不能改图谱**：

    search_nodes(keyword, node_type)      找候选节点

为什么只有 1 个工具
------------------
原来还给了 `get_node` / `expand_node` / `find_path`，实测 6 组不同输入，
LLM 只用 `search_nodes` 和最后的 `select_nodes`，那三个的调用次数是 1 / 0 / 0。
而它们每次都塞进 tool schema 占 prompt 额度（一千多字符），还会让 LLM 多绕轮次。

`search_nodes` 已经覆盖了这三件事：一次就返回节点名、类型、命中方式、别名数，
够 LLM 判断该选哪些。

设计原则
--------
1. **全部只读** —— LLM 在医疗场景绝不能改知识库，写入必须走离线 + 人工审核。
2. **返回纯 JSON** —— 工具结果要喂回给 LLM，必须是可序列化的 dict。
3. **结果带上「为什么匹配」** —— 便于 LLM 判断，也便于事后审计。
4. **限制返回量** —— 防止把几万条结果塞进 prompt。
5. **不依赖 Django** —— 直接用 pymysql，本模块可独立运行。

用法
----
    from store import get_store
    s = get_store()
    s.search_nodes("腿疼")
    s.validate_keys(["symptom:腰痛"])      # 校验 LLM 给的 key 是否真实存在
"""

import re
import json
import logging

import db

logger = logging.getLogger(__name__)

# 单次返回上限，防止把 prompt 撑爆
MAX_SEARCH = 12


def _node_brief(row, match=None):
    """把一行 kg_node 记录转成给 LLM 看的最小信息"""
    d = {
        "node_key": row["node_key"],
        "type": row["node_type"],
        "name": row["name"],
    }
    if match:
        d["match"] = match
    extra = row.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    extra = extra or {}
    # 只透出对决策有用的属性
    for k in ("location", "prep", "parent", "bookable"):
        if extra.get(k):
            d[k] = extra[k]
    return d


class KgStore:
    """
    知识图谱只读操作。

    所有方法都返回**可 JSON 序列化**的结构，报错时返回带 error 字段的 dict，
    不抛异常 —— 因为工具是给 LLM 调的，异常会中断整个工具调用循环。
    """

    # ------------------------------------------------------------------
    # 工具 1：找候选节点
    # ------------------------------------------------------------------
    def search_nodes(self, keyword, node_type=None, limit=MAX_SEARCH):
        """
        按关键词找节点。四路召回，按可信度排序：

          1. 别名精确匹配   「腿疼」→ symptom:腰痛        ← 最可信
          2. 名称精确匹配   「腰痛」→ symptom:腰痛
          3. 名称包含匹配   「痛」  → 含「痛」的所有节点
          4. 切词兜底       长句自动切片段再搜            ← 解决 LLM 整句搜索

        返回: {"keyword":..., "count":..., "nodes":[节点摘要...]}
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return {"keyword": "", "count": 0, "nodes": [], "error": "关键词为空"}

        limit = max(1, min(int(limit or MAX_SEARCH), MAX_SEARCH))
        found = {}          # node_key -> (优先级, row, 匹配方式)

        def add(row, prio, match):
            k = row["node_key"]
            if k not in found or prio < found[k][0]:
                found[k] = (prio, row, match)

        type_sql, type_args = ("", ())
        if node_type:
            type_sql, type_args = (" AND n.node_type=%s", (node_type,))

        # 1) 别名精确匹配（最高优先级）
        rows = db.query(
            "SELECT n.*, 'alias' AS _m FROM kg_alias a "
            "JOIN kg_node n ON n.node_key=a.node_key "
            "WHERE a.alias=%s" + type_sql + " LIMIT %s",
            (keyword,) + type_args + (limit * 3,))
        for r in rows:
            add(r, 0, "alias")

        # 2) 名称精确匹配
        rows = db.query(
            "SELECT n.* FROM kg_node n WHERE n.name=%s" + type_sql + " LIMIT %s",
            (keyword,) + type_args + (limit,))
        for r in rows:
            add(r, 1, "name_exact")

        # 3) 名称包含匹配（keyword 是节点名的一部分）
        if len(found) < limit:
            rows = db.query(
                "SELECT n.* FROM kg_node n WHERE n.name LIKE %s" + type_sql + " LIMIT %s",
                ("%" + keyword + "%",) + type_args + (limit * 2,))
            for r in rows:
                add(r, 2 + min(len(r["name"]), 20) / 100.0, "name_partial")

        # 3.5) ★ 剥掉口语填充词：整句搜不到时，先去掉「我 / 最近 / 有点」这类
        #      修饰成分再试一次。实测：「我腿疼」直接搜 0 命中，剥掉「我」就命中「腿疼」。
        if not found and len(keyword) >= 3:
            for alt in self._strip_filler(keyword)[1:]:      # [0] 是原文，跳过
                rows = db.query(
                    "SELECT n.* FROM kg_alias a JOIN kg_node n ON n.node_key=a.node_key "
                    "WHERE a.alias=%s" + (type_sql.replace("n.node_type", "n.node_type")),
                    (alt,) + type_args)
                if not rows:
                    continue
                for r in rows:
                    add(r, 0.5, "alias_stripped")
                break
            if found:
                stripped = [a for a in self._strip_filler(keyword)]
                keyword_used = stripped[1] if len(stripped) > 1 else keyword
            else:
                keyword_used = keyword
        else:
            keyword_used = keyword

        # 4) ★ 切词兜底：整句搜不到时，从长句里切出片段再搜。
        #    实测问题：患者说「我这两天走路腿沉，还发麻」，LLM 会把**整句**
        #    当关键词来搜，结果 0 命中，反复重试直到超轮次。
        chopped = []
        # ⚠️ 阈值不能太高。实测踩坑：设为 >4 时，「我腿疼」（3 字）不会切词，
        #    导致整句匹配失败、返回 0 命中 —— 而这只是多了一个人称代词。
        #    只要长度 >= 3 就切（2 字没有可切的余地，无意义）。
        if not found and len(keyword) >= 3:
            chopped = self._chop(keyword)
            if chopped:
                like_clause = " OR ".join(["n.name LIKE %s"] * len(chopped))
                args = tuple("%" + w + "%" for w in chopped) + type_args + (limit * 3,)
                rows = db.query(
                    "SELECT n.* FROM kg_node n WHERE (" + like_clause + ")" + type_sql
                    + " LIMIT %s", args)
                for r in rows:
                    # 越长的窗口命中越具体，可信度越高
                    wlen = max((len(w) for w in chopped if w in r["name"]), default=2)
                    add(r, 3 + (5 - wlen) / 10.0, "chopped")

        ordered = sorted(found.values(), key=lambda x: x[0])[:limit]
        out = {
            "keyword": keyword,
            "count": len(ordered),
            "nodes": [_node_brief(r, m) for _, r, m in ordered],
        }
        if keyword_used != keyword:
            out["normalized_from"] = keyword_used
        if chopped:
            out["chopped_into"] = chopped[:12]
        if not ordered:
            # 给 LLM 明确提示，避免它反复用同一个词重试
            out["hint"] = (
                "没有找到匹配节点。请改用更短、更常见的医学词重试，"
                "例如把「走路腿沉」改成「腿沉」或「下肢麻木」；"
                "如果确实没有相关概念，请调用 select_nodes 提交空数组。"
            )
        return out

    # 常见虚词，用于过滤无意义的 2 字窗口
    _STOP_CHARS = set("我你他她它这那了的还也就有是在和与及或很太非常一二三四五六七八九十两"
                      "多少天日月年时分秒个之其而且但就是否没不有能会要想去来着过")

    # 口语里常见的填充词。患者说「我腿疼」「最近牙疼」「眼睛有点疼」，
    # 直接拿去匹配节点名会全军覆没，需要先剥掉。
    # ⚠️ 只剥「修饰性」的词，不碰否定词（没/不/无）—— 那会改变语义。
    _FILLER_PREFIX = (
        "我", "你", "他", "她", "它", "我们", "你们", "咱们",
        "最近", "这两天", "这几天", "这几天来", "前几天", "今天", "昨天", "这几天",
        "一直", "总是", "老是", "经常", "偶尔", "突然",
        "感觉", "觉得", "有点", "有些", "比较", "非常", "特别", "稍微", "大概", "好像",
        "现在", "目前", "近来",
    )
    _FILLER_SUFFIX = ("了", "的了", "的样子", "感觉", "这种", "这样", "一下")

    @classmethod
    def _strip_filler(cls, text):
        """剥掉首尾的口语填充词，返回候选列表（含剥与不剥两种，按优先顺序）"""
        out = [text]
        t = text
        changed = True
        while changed and len(t) > 2:
            changed = False
            for f in cls._FILLER_PREFIX:
                if t.startswith(f) and len(t) - len(f) >= 2:
                    t = t[len(f):]
                    out.append(t)
                    changed = True
                    break
            else:
                for f in cls._FILLER_SUFFIX:
                    if t.endswith(f) and len(t) - len(f) >= 2:
                        t = t[:-len(f)]
                        out.append(t)
                        changed = True
                        break
        return list(dict.fromkeys(out))

    @classmethod
    def _chop(cls, text, min_len=2, max_len=4, max_parts=60):
        """
        从长句里切出候选片段。

        策略：
          1. 按标点/连接词切分成小段
          2. 每段做 2~4 字滑窗（中文医学词多为 2 字，如「发麻」「腿沉」「胸闷」）
          3. 过滤掉「整词都是虚词」的窗口（如「我这」「还有」）
          4. 长窗口排前面（更具体），总量设上限避免 SQL 过长
        """
        parts = [p for p in re.split(r"[，。、,.;；:：!！?？\s（）()【】\[\]「」]+", text) if p]

        long_w, mid_w, short_w = [], [], []
        for p in parts:
            if len(p) <= max_len:
                (mid_w if len(p) >= 3 else short_w).append(p)
            for n in range(max_len, min_len - 1, -1):
                if len(p) < n:
                    continue
                for i in range(len(p) - n + 1):
                    w = p[i:i + n]
                    if all(ch in cls._STOP_CHARS for ch in w):
                        continue
                    if n == max_len:
                        long_w.append(w)
                    elif n == max_len - 1:
                        mid_w.append(w)
                    else:
                        short_w.append(w)

        uniq, seen = [], set()
        for w in long_w + mid_w + short_w:      # 长窗口优先
            if w not in seen:
                seen.add(w)
                uniq.append(w)
        return uniq[:max_parts]

    # ------------------------------------------------------------------
    # 辅助（非工具）
    # ------------------------------------------------------------------
    def validate_keys(self, keys):
        """
        校验一批 node_key 是否真实存在。

        ⭐ 这是「LLM 选节点，规则验节点」的落地点 ——
           LLM 可能编出不存在的 key，必须过滤掉。
        """
        keys = [k for k in (keys or []) if k]
        if not keys:
            return {"valid": [], "invalid": []}
        ph = ",".join(["%s"] * len(keys))
        rows = db.query("SELECT node_key FROM kg_node WHERE node_key IN (" + ph + ")",
                        tuple(keys))
        exist = {r["node_key"] for r in rows}
        return {
            "valid": [k for k in keys if k in exist],
            "invalid": [k for k in keys if k not in exist],
        }

    def get_names(self, keys):
        """批量取名字，返回 {node_key: (name, type)}"""
        keys = [k for k in (keys or []) if k]
        if not keys:
            return {}
        ph = ",".join(["%s"] * len(keys))
        rows = db.query("SELECT node_key, name, node_type FROM kg_node "
                        "WHERE node_key IN (" + ph + ")", tuple(keys))
        return {r["node_key"]: (r["name"], r["node_type"]) for r in rows}

    def stats(self):
        """图谱规模统计"""
        counts = db.table_counts()
        return {"nodes": counts.get("kg_node", 0),
                "edges": counts.get("kg_edge", 0),
                "aliases": counts.get("kg_alias", 0)}

    # ------------------------------------------------------------------
    # 供 LLM 使用的 JSON Schema（OpenAI function calling 格式）
    # ------------------------------------------------------------------
    @staticmethod
    def tool_schemas():
        """工具的 OpenAI function calling 定义"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_nodes",
                    "description": (
                        "在医学知识图谱里按关键词查找节点。"
                        "支持口语表达（如「腿疼」会自动匹配到规范术语「腰痛」），"
                        "也支持直接传整句（会自动切词）。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {"type": "string", "description": "要查找的关键词，可直接传患者的原话"},
                            "node_type": {
                                "type": "string",
                                "enum": ["symptom", "disease", "department", "check_item", "doctor"],
                                "description": "限定节点类型，不填则搜全部",
                            },
                        },
                        "required": ["keyword"],
                    },
                },
            },
        ]

    # ------------------------------------------------------------------
    def call(self, name, args):
        """
        按名字调度工具（供 agent 用）。

        参数错误或工具异常都返回 {"error":...}，不抛异常 ——
        异常会中断 LLM 的工具调用循环。
        """
        args = args or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception as e:
                return {"error": "参数不是合法 JSON: %s" % e}

        funcs = {
            "search_nodes": self.search_nodes,
        }
        fn = funcs.get(name)
        if not fn:
            return {"error": "未知工具: %s" % name, "available": list(funcs.keys())}
        try:
            return fn(**args)
        except TypeError as e:
            return {"error": "参数不匹配: %s" % e}
        except Exception as e:
            logger.exception("[KgStore] 工具 %s 执行失败", name)
            return {"error": "执行失败: %s" % str(e)[:200]}


# ----------------------------------------------------------------------
# 单例
# ----------------------------------------------------------------------
_store = None


def get_store():
    global _store
    if _store is None:
        _store = KgStore()
    return _store


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    s = get_store()
    print("图谱规模:", s.stats())
    print()

    print("=== search_nodes('腿疼') ===")
    r = s.search_nodes("腿疼")
    for n in r["nodes"]:
        print("   %-14s %-8s (%s)" % (n["name"], n["type"], n["match"]))
    print()

    print("=== 长句切词 ===")
    r = s.search_nodes("我这两天走路腿沉，还发麻")
    print("   切词:", (r.get("chopped_into") or [])[:6])
    print("   命中:", [n["name"] for n in r["nodes"]])
    print()

    print("=== validate_keys（校验 LLM 给的 key 是否真实存在）===")
    r = s.validate_keys(["symptom:腰痛", "symptom:不存在的症状"])
    print("   valid  :", r["valid"])
    print("   invalid:", r["invalid"])
