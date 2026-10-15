# -*- coding: utf-8 -*-
"""
路径中心
========

全模块的目录定位**只在这里算一次**，其他地方 `from paths import ...`。

为什么需要它
------------
原来有 8 处各算各的，用的都是「往上数几层」：

    _PROJ = dirname(dirname(_HERE))          # 假设 kg 在 <项目>/tests/kg

只要把 `tests/kg` 挪个位置（比如并入主项目时改成 `<项目>/kg`），
这些全都**静默算错** —— 路径错了不报错，只会找不到文件，
表现为 `integrity` 那 28 个用例悄悄变成 0 个。

现在的做法是**找标记**而不是数层数：从本文件往上走，哪个目录里
有 `LLM_server/` 就认为它是项目根。放哪都对。

独立部署（把 tests/kg 单独拷走、没有上层项目）时 PROJECT_ROOT 是 None，
DATA_DIR 回落到模块自己目录下的 `data/`。
"""

import os

# 本模块目录（tests/kg/）
KG_DIR = os.path.dirname(os.path.abspath(__file__))

# 项目根的标记：项目根下一定有这个目录
_ROOT_MARKER = "LLM_server"
_MAX_UP = 6


def _find_project_root(start):
    """从 start 往上找含 LLM_server/ 的目录，找不到返回 None"""
    d = start
    for _ in range(_MAX_UP):
        if os.path.isdir(os.path.join(d, _ROOT_MARKER)):
            return d
        parent = os.path.dirname(d)
        if parent == d:          # 到盘符根了
            break
        d = parent
    return None


# 项目根。独立部署时为 None —— 用到它的地方都要能接受 None
PROJECT_ROOT = _find_project_root(KG_DIR)

# 数据目录：优先项目根的 data/，独立部署时用模块自己的 data/
DATA_DIR = os.path.join(PROJECT_ROOT, "data") if PROJECT_ROOT \
    else os.path.join(KG_DIR, "data")

# .env 查找顺序：模块自带的最优先，其次兼容 Django 那边的
ENV_CANDIDATES = [os.path.join(KG_DIR, ".env")]
if PROJECT_ROOT:
    ENV_CANDIDATES.append(os.path.join(PROJECT_ROOT, "LLM_server", ".env"))


def load_env():
    """
    把 .env 读进 os.environ。**幂等**，且不覆盖已设的环境变量。

    为什么要单独抽出来：`db.py`、`config.py`、`recommend.py` 都要读 .env。
    早先只有 `db.py` 会加载，其它两个靠「调用顺序恰好先碰到 db」才拿到配置 ——
    单独 `import config` 就会读不到 API Key。现在谁先用到谁调一次，都一样。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    for p in ENV_CANDIDATES:
        if os.path.exists(p):
            load_dotenv(p, override=False)
            return True
    return False


def describe():
    """给自检用：一眼看出路径算得对不对"""
    return {
        "KG_DIR": KG_DIR,
        "PROJECT_ROOT": PROJECT_ROOT,
        "DATA_DIR": DATA_DIR,
        "DATA_DIR 存在": os.path.isdir(DATA_DIR),
        ".env 候选": [p for p in ENV_CANDIDATES if os.path.exists(p)] or "（一个都没有）",
    }


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("路径解析结果：")
    for k, v in describe().items():
        print("  %-14s %s" % (k, v))
