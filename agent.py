# -*- coding: utf-8 -*-
"""
LLM 节点选择器（第 2 层）
========================

让 LLM**从知识图谱里挑节点**，然后把节点交给图遍历出结果。

核心约束（医疗场景的硬要求）
----------------------------
    LLM 只能「选」，不能「造」。

        LLM 提议 node_key  →  图谱校验是否真的存在
                                 ├─ 存在   → 采纳
                                 └─ 不存在 → 丢弃

    LLM 说不出图谱里没有的东西，因为每个 key 都要过校验。

三层执行顺序
------------
    ① 规则层   别名表精确匹配       最快，零成本，命中即返回
    ② LLM 层   工具调用选节点        处理别名表没覆盖的表达
    ③ 无结果   返回空               由上层决定如何回答（本项目答「不确定」）

为什么 LLM 层用轻量模型（qwen3.8-flash 而非 max）：
    工具调用是「从候选里选」，不是开放推理，有双重约束（候选列表 + 图谱校验）
    把模型能力的影响压扁了。而它每轮对话要调 2~3 次，用大模型是纯浪费。

用法
----
    from agent import get_selector
    sel = get_selector()
    r = sel.select("我腿疼，还有点麻")
    # {"nodes": ["symptom:腰痛", "symptom:下肢麻木"],
    #  "source": "llm", "trace": [...], "elapsed_ms": 1234}
"""

import json
import time
import logging

import config as llm_config
import prompts
from store import get_store

logger = logging.getLogger(__name__)

# 最多几轮工具调用（防止 LLM 无限循环）
MAX_ROUNDS = 6

# 最终输出工具：不是图谱操作，是「协议工具」，让 LLM 用它交答案
FINALIZE_TOOL = {
    "type": "function",
    "function": {
        "name": "select_nodes",
        "description": (
            "当你已经确定了与患者描述最相关的图谱节点后，调用本工具提交结果。"
            "必须在查完节点后调用，否则无法给出结果。"
            "如果确实找不到任何相关节点，提交空数组。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "选中的节点键列表，如 [\"symptom:腰痛\"]，最多 5 个",
                },
                "reason": {"type": "string", "description": "简要说明为什么选这些节点"},
            },
            "required": ["node_keys"],
        },
    },
}

# 节点选择器的 system prompt 不写在这里，放在 prompts/select_nodes.md
#
# 改提示词只要改那个 .md 文件，不用动这个文件（也没有缓存，改完立即生效）。
# 为什么不在代码里留一份默认值：那样会有两份提示词，改了一份另一份不生效，
# 这类问题极难排查。详见 prompts.py 的说明。
#
# 查看当前提示词：python tests/kg/prompts.py
PROMPT_NAME = "select_nodes"


class NodeSelector:
    """把患者文本转换成图谱节点"""

    def __init__(self):
        self.store = get_store()

    # ------------------------------------------------------------------
    # ① 规则层：别名表精确匹配
    # ------------------------------------------------------------------
    # 规则层接受哪些匹配方式（其余交给 LLM）
    #
    #   alias          别名精确匹配        「腿疼」→ 腰痛            ← 最可信
    #   name_exact     节点名精确匹配      「腰痛」→ 腰痛
    #   alias_stripped 剥掉填充词后精确匹配 「我腿疼」→ 腰痛          ← 实测补充
    #
    # ⚠️ 故意**不**接受下面两种，因为它们不够确定，交给 LLM 效果更好：
    #   chopped        切词子串命中  「我这两天走路腿沉还发麻」→ 手脚发麻/脚趾发麻
    #   name_partial   名称包含命中  「痛」→ 一大堆含「痛」的节点
    RULE_MATCH_TYPES = ("alias", "name_exact", "alias_stripped")

    def _rule_match(self, text, limit=5):
        """
        最快路径：口语表达能精确落到某个节点时直接返回，不调用 LLM。
        省时省钱（10ms vs 1~2s），且结果 100% 确定。
        """
        text = (text or "").strip()
        if not text:
            return []

        keys = []
        r = self.store.search_nodes(text, limit=limit * 2)
        for n in r.get("nodes", []):
            if n.get("match") in self.RULE_MATCH_TYPES:
                if n["node_key"] not in keys:
                    keys.append(n["node_key"])
            if len(keys) >= limit:
                break
        return keys[:limit]

    # ------------------------------------------------------------------
    # ② LLM 层：工具调用
    # ------------------------------------------------------------------
    def _llm_select(self, text, max_rounds=MAX_ROUNDS):
        """
        让 LLM 用工具查图并选节点。

        返回 (node_keys, trace, note)
        note 非空表示走不通（无 LLM / 超轮次 / 未提交）
        """
        trace = []

        if not llm_config.is_enabled():
            return [], trace, "LLM_ENABLED=False，跳过 LLM 层"

        ok, problems = llm_config.validate()
        if not ok:
            return [], trace, "LLM 配置不可用: %s" % "; ".join(problems)

        # 工具调用需要确定性输出：温度设 0
        llm = llm_config.get_chat_model("fast", temperature=0, streaming=False)
        tools = self.store.tool_schemas() + [FINALIZE_TOOL]
        bound = llm.bind_tools(tools)

        messages = [
            # 每次都重新读文件 → 改 prompts/select_nodes.md 后立即生效，不用重启
            {"role": "system", "content": prompts.load(PROMPT_NAME)},
            {"role": "user", "content": "患者主诉：%s" % text},
        ]

        for rnd in range(1, max_rounds + 1):
            try:
                resp = bound.invoke(messages)
            except Exception as e:
                logger.warning("[Selector] LLM 调用失败(第%d轮): %s", rnd, e)
                return [], trace, "LLM 调用失败: %s" % str(e)[:150]

            calls = getattr(resp, "tool_calls", None) or []

            if not calls:
                trace.append({"round": rnd, "note": "LLM 未调用工具，返回文本",
                              "content": (resp.content or "")[:120]})
                return [], trace, "LLM 未按要求调用工具"

            # 把 LLM 这一轮的发言加入历史
            messages.append(resp)

            for tc in calls:
                name = tc.get("name")
                args = tc.get("args") or {}

                # —— 终局：LLM 提交结果 ——
                if name == "select_nodes":
                    raw = args.get("node_keys") or []
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw)
                        except Exception:
                            raw = [raw]
                    v = self.store.validate_keys(raw)
                    trace.append({"round": rnd, "tool": "select_nodes",
                                  "submitted": raw, "valid": v["valid"],
                                  "invalid": v["invalid"],
                                  "reason": (args.get("reason") or "")[:120]})
                    if v["invalid"]:
                        logger.warning("[Selector] LLM 提交了 %d 个不存在的节点，已丢弃: %s",
                                       len(v["invalid"]), v["invalid"][:3])
                    return v["valid"][:5], trace, ""

                # —— 图查询工具：执行并把结果喂回去 ——
                result = self.store.call(name, args)
                brief = self._brief_result(name, args, result)
                trace.append({"round": rnd, "tool": name,
                              "args": args, "result_brief": brief})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": json.dumps(result, ensure_ascii=False)[:4000],
                })

        return [], trace, "超过 %d 轮工具调用仍未提交结果" % max_rounds

    @staticmethod
    def _brief_result(name, args, result):
        """把工具结果压成一行，放进 trace 便于排查"""
        if "error" in result:
            return "错误: %s" % result["error"]
        if name == "search_nodes":
            return "找到 %d 个: %s" % (
                result.get("count", 0),
                [n["name"] for n in result.get("nodes", [])][:5])
        if name == "select_nodes":
            return "提交 %d 个，有效 %d 个" % (
                len(result.get("submitted") or []), len(result.get("valid") or []))
        return str(result)[:120]

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def select(self, text, use_llm=True):
        """
        把文本转成图谱节点。

        返回:
            {
              "nodes":      [node_key, ...]      选中的节点（已通过图谱校验）
              "source":     "rule" | "llm" | "none"
              "trace":      [...]                调试/审计线索
              "elapsed_ms": 1234
              "note":       "..."                走不通时的原因说明
            }
        """
        t0 = time.time()
        text = (text or "").strip()

        if not text:
            return {"nodes": [], "source": "none", "trace": [],
                    "elapsed_ms": 0, "note": "输入为空"}

        # ① 规则层
        keys = self._rule_match(text)
        if keys:
            return {
                "nodes": keys, "source": "rule",
                "trace": [{"stage": "rule", "hit": keys}],
                "elapsed_ms": int((time.time() - t0) * 1000), "note": "",
            }

        # ② LLM 层
        trace = []
        if use_llm:
            keys, trace, note = self._llm_select(text)
            if keys:
                return {
                    "nodes": keys, "source": "llm", "trace": trace,
                    "elapsed_ms": int((time.time() - t0) * 1000), "note": "",
                }
        else:
            note = "调用方要求跳过 LLM"

        # ③ 无结果
        return {
            "nodes": [], "source": "none", "trace": trace,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "note": note or "规则层与 LLM 层都未匹配到节点",
        }


_selector = None


def get_selector():
    global _selector
    if _selector is None:
        _selector = NodeSelector()
    return _selector


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    sel = get_selector()
    print(llm_config.describe())
    print()

    print("=== 规则层（不调 LLM）===")
    for text in ["腿疼", "腰没有疼"]:
        r = sel.select(text, use_llm=False)
        print("  【%s】source=%s  nodes=%s  (%dms)"
              % (text, r["source"], r["nodes"], r["elapsed_ms"]))

    if "--llm" in sys.argv:
        print()
        print("=== LLM 层 ===")
        for text in ["心口像压了块石头", "我这两天走路腿沉，还发麻"]:
            r = sel.select(text, use_llm=True)
            print("  【%s】" % text)
            print("      source=%s  nodes=%s" % (r["source"], r["nodes"]))
            for t in r.get("trace", []):
                nm = t.get("tool") or t.get("stage") or t.get("note") or ""
                detail = t.get("result_brief") or t.get("reason") or ""
                print("      → %s %s" % (nm, detail[:70]))
            print()
