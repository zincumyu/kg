# -*- coding: utf-8 -*-
"""
真实用户输入的调用返回
======================

运行：
    python tests/kg/fctest/example_call.py           # 只演示短词（快，约 2 秒）
    python tests/kg/fctest/example_call.py --llm     # 带上整句（调 LLM，约 10 秒/句）

用户输入分两类，走的路径完全不同：

    短词/口语  「腿疼」「拉肚子」        → 规则层命中，约 20 毫秒，不花钱
    整句主诉   「我这两天肚子不舒服」    → 规则层不命中，转 LLM，约 10 秒

整个调用就两步：

    sel = get_selector().select(用户输入)     # ① 文本 → 节点
    r   = traverse(sel["nodes"], age=年龄)    # ② 节点 → 科室
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # tests/kg

from agent import get_selector        # noqa: E402
from recommend import traverse        # noqa: E402


def show(text, age=None, use_llm=False):
    print("\n" + "#" * 72)
    print("#  输入：%s" % text)
    print("#" * 72)

    # ---------------- ① 文本 → 节点 ----------------
    sel = get_selector().select(text, use_llm=use_llm)
    print("\n① select() 返回：")
    print(json.dumps(sel, ensure_ascii=False, indent=2))

    if not sel["nodes"]:
        print("\n② 没有节点，traverse() 不调用")
        print("\n结论：走「不确定」分支，不推荐科室")
        return

    # ---------------- ② 节点 → 科室 ----------------
    r = traverse(sel["nodes"], top_n=3, age=age)
    print("\n② traverse() 返回：")
    print(json.dumps(r, ensure_ascii=False, indent=2))

    if r["departments"]:
        top = r["departments"][0]
        print("\n结论：推荐 %s（置信度 %.2f，%s）"
              % (top["科室"], top["置信度"],
                 "够" if r["enough"] else "不够，上层应拒答"))
    else:
        print("\n结论：走「不确定」分支，不推荐科室")


def main():
    use_llm = "--llm" in sys.argv

    print("=" * 72)
    print("  短词输入（规则层，约 20 毫秒）")
    print("=" * 72)
    show("腿疼")
    show("拉肚子")
    show("今天天气真好")

    if use_llm:
        print("\n\n" + "=" * 72)
        print("  整句主诉（规则层不命中 → 转 LLM，约 10 秒）")
        print("=" * 72)
        show("我这两天肚子不舒服，还老想吐", age=30, use_llm=True)
        show("心口像压了块石头", age=60, use_llm=True)
    else:
        print("\n\n（加 --llm 可看整句主诉走 LLM 的完整返回，每句约 10 秒）")

    # ---------------- 网站拿到的最终结果 ----------------
    print("\n\n" + "=" * 72)
    print("  网站（Django）最终拿到的结构")
    print("=" * 72)
    print("""
medical_agent.py 里把 traverse() 的结果包成挂号卡片，前端收到的是：

    {
        "步骤": "前往骨科专科就诊",
        "科室": "骨科专科",
        "科室键": "department:骨科专科",
        "原因": "「腿疼」可能表现为 ...，建议到骨科专科就诊",
        "其他可能科室": ["泌尿专科", "妇科专科"],
        "相关疾病": ["关节退行性变", "胸腰椎骨折", ...],
        "票数": 5,
        "置信度": 0.3333
    }

置信度低于 KG_MIN_CONFIDENCE（默认 0.15）时，
summary_agent 会改口回答「不确定，建议线下咨询」，不推科室。
""")


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    main()
