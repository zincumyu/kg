# -*- coding: utf-8 -*-
"""
提示词加载器
============

提示词**不写在代码里**，放在 `prompts/` 目录下，一个用途一个文件：

    prompts/select_nodes.md  →  load("select_nodes")

改提示词只要改 `.md` 文件，**不用动 Python 代码，也不用重启服务**
（每次调用都重新读文件，方便调试）。

为什么用 `.md` 文件而不是写在代码里 / 写进 `.env`：

    * 提示词是**多行长文本**，塞进 .env 要转义换行，没法看 diff
    * 写成 .md 可以直接给医学审核人员改，不用碰 Python
    * 放在 git 里能追溯「哪次改动让推荐变差了」

为什么不用数据库存：

    * 提示词变更频率低，但需要**版本可追溯**，git 比数据库表合适
    * 数据库里的 prompt 没法跟着代码一起分支/回滚

⚠️ `prompts/` 目录是**程序的一部分**，不要删。
   删了会直接报 FileNotFoundError（这是故意的，见 load() 的注释）。
"""

import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def path(name):
    """提示词文件的完整路径，便于报错和排查"""
    return os.path.join(_DIR, name + ".md")


def load(name):
    """
    读取提示词，返回去掉首尾空白的字符串。
    ⚠️ 文件不存在时**故意抛异常**，而不是回落到代码里的默认值。
       如果回落到代码，就会出现「两份提示词」，改了一份另一份不生效 ——
       这种问题极难排查。宁可启动就报错，也不要静默用错提示词。
    """
    p = path(name)
    if not os.path.exists(p):
        raise FileNotFoundError(
            "提示词文件不存在：%s\n"
            "提示词是程序的一部分，请从 git 恢复 tests/kg/prompts/ 目录。" % p)

    with open(p, encoding="utf-8") as f:
        text = f.read().strip()

    if not text:
        raise ValueError("提示词文件是空的：%s" % p)

    return text


def list_all():
    """列出所有可用的提示词名 去掉 .md 便于排查「怎么少了一个」"""
    if not os.path.isdir(_DIR):
        return []
    return sorted(f[:-3] for f in os.listdir(_DIR) if f.endswith(".md"))


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("提示词目录：%s" % _DIR)
    print()
    names = list_all()
    if not names:
        print("⚠️  目录下没有任何 .md 文件")
    for n in names:
        body = load(n)
        print("  %-14s %3d 行 / %4d 字" % (n, len(body.splitlines()), len(body)))
    print()
    print("修改后立即生效，不用重启（每次调用都重新读文件）。")
