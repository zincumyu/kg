# -*- coding: utf-8 -*-
"""
功能测试包（fctest）
====================

测试程序全部放在这里，`tests/kg/` 顶层只留图谱程序本身。
**整个 `fctest/` 目录删掉也不影响网站运行。**

这个 `__init__.py` 只做一件事：把两个目录加进 `sys.path`，
使得下面两种调用方式都能正常工作：

    python tests/kg/fctest/run_tests.py                     # 推荐
    python -m unittest tests.kg.fctest.test_kg_nodes -v     # 包路径方式

为什么要两个目录：
    tests/kg/          提供 db / store / agent / recommend / config
    tests/kg/fctest/   提供 test_config（各测试文件共用）
"""

import os
import sys

_FCTEST_DIR = os.path.dirname(os.path.abspath(__file__))
_KG_DIR = os.path.dirname(_FCTEST_DIR)                    # tests/kg/

for _p in (_KG_DIR, _FCTEST_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
