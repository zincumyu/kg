# -*- coding: utf-8 -*-
"""
知识图谱测试包
==============

把本目录加入 sys.path，使得各测试文件可以直接 `from test_config import ...`，
无论用哪种方式运行（`python -m unittest` / 直接执行文件 / IDE）都能正确导入。

运行方式见 ./README.md
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
