# -*- coding: utf-8 -*-
"""
知识图谱调用最小演示
====================

不管什么输入，调用都只有两步：

    ① select(文本)     →  节点
    ② traverse(节点)    →  科室

运行：
    python tests/kg/demo.py              # 跑下面两个内置例子
    python tests/kg/demo.py 腿疼          # 查一句话
    echo 牙疼 | python tests/kg/demo.py   # 也支持管道

退出码：0=有推荐，1=不确定，2=没给输入

两个内置例子走的是两条完全不同的路：

    example 1「腿痛」              规则层命中，约 20 毫秒，不调 LLM
    example 2 整句主诉             规则层不命中，转 LLM，约 10 秒

需要完整演示（五部分，含数据规模、图遍历原理等）→ 跑 tests/kg/demo_full.py

注：例 1 的置信度是 1.00，看着很高，其实是个陷阱 ——
    symptom:腿痛 在数据集里只连着 1 个疾病（风湿性关节炎），
    只有 1 个候选，投票必然全票通过。置信度是「相对投票比例」，不是准确率。
    换成「腿疼」会走另一条别名，推荐骨科专科，医学上更合理。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import get_selector        # noqa: E402
from recommend import traverse        # noqa: E402

selector = get_selector()

EXAMPLE_1 = "腿痛"
EXAMPLE_2 = "我经常熬夜加班,最近嘴唇有点长泡,感觉上火了"


def run(text):
    """跑一次完整调用，把每一步都打出来。返回是否拿到了科室推荐。"""

    print("输入 :", text)

    # ---------- ① 文本 → 节点 ----------
    result = selector.select(text)
    print("  ①节点 :", result["nodes"])
    print("    来源 : %s      耗时 : %d 毫秒" % (result["source"], result["elapsed_ms"]))

    # 没有节点 → 上层应该回答「不确定」，不推科室
    if not result["nodes"]:
        print("  ②科室 : 没有节点，回答「不确定」")
        return False

    # ---------- ② 节点 → 科室 ----------
    deps = traverse(result["nodes"])
    for d in deps["departments"]:
        print("  ②科室 : %-10s 置信度 %.2f" % (d["科室"], d["置信度"]))
    if not deps["departments"]:
        print("  ②科室 : 不确定")
        return False
    return True


def main():
    # 命令行参数优先；没有参数但接了管道就读 stdin；都没有才跑内置例子
    text = " ".join(sys.argv[1:]).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()

    if text:
        print("=" * 64)
        return 0 if run(text) else 1

    print("=" * 64)
    print("example 1 ")
    ok1 = run(EXAMPLE_1)
    print()

    print("=" * 64)
    print("example 2")
    print("调用模型中")
    ok2 = run(EXAMPLE_2)
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
