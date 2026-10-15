# tests/kg —— 知识图谱模块

把患者的一句话变成「该挂哪个科」。不依赖 Django，用 pymysql 直连 MySQL。

```
输入  腿疼
输出  骨科专科（置信度 0.33）
```

完整文档见 [dev-help/dev-help.md](dev-help/dev-help.md)。

# 快速开始

## 项目地址
最好是`\Django_project_stream_medical\tests\kg`

母项目文件夹`\Django_project_stream_medical`

新建`\tests`,然后在此地址git 本项目
## 1. 环境

1. **Python 环境** —— 母项目的 `workflows_env`,用相同的库

2. **MySQL** —— 库 `ai250301`，表和数据由 `database/` 下两个 SQL 提供，见下节

3. **`tests/kg/.env`** —— 配 `LLM_API_KEY` 和 `DB_*`
   不配也能跑，但不调 LLM（整句会答「不确定」）。
   模板见 `.env.example`

## 2. 数据库

库 `ai250301`。两个 SQL 在 `database/`，docker 首次启动时自动导入
（**只在 `mysql-data` 为空时跑**）：

| 文件 | 大小 | 内容 |
|---|---|---|
| `database/medical.sql` | 2.2 MB | 22 张业务表（`aichat_*` / `auth_*` / `user_*` / 排班表…） |
| `database/kg_graph.sql` | 19 MB | **KG 三张表 + 全部数据** |

**拉入原项目SQL**

```powershell
#先在母文件夹\Django_project_stream_medical\
docker compose -f docker/mysql_docker/docker-compose.yml up -d   # 两个 .sql 自动导入
cd LLM_server
python manage.py migrate                                          # No migrations to apply
cd ..
.\start_website.ps1
```
`migrate` 会显示 `No migrations to apply`。


> 这两份 SQL 怎么重新生成、为什么 `medical.sql` 能瘦到 2.2 MB —— 见 [dev-help/dev-help.md](dev-help/dev-help.md)。

## 3. 跑起来

在项目根目录执行。先激活项目环境（只需一次）：

```powershell
conda activate workflows_env

python tests/kg/demo.py                        # 最小演示
python tests/kg/demo.py 腿疼                   # 查一句话
python tests/kg/fctest/run_tests.py            # 跑测试（125 用例）
```


## 目录结构

```
tests/kg/
├── db.py              MySQL 访问层（pymysql，无 ORM）
├── store.py           图谱检索工具：search_nodes（只读）+ validate_keys
├── agent.py           节点选择器：规则层 → LLM 层
├── recommend.py       图遍历 + 加权投票 → 科室
├── config.py          LLM 配置中心（读 .env）
├── paths.py           路径中心（项目根/数据目录/.env 只在这里算）
├── prompts.py         提示词加载器（无缓存）
├── prompts/
│   └── select_nodes.md    节点选择器的 system prompt（改完立即生效）
├── kgapi.py           只读查询辅助（查三张表用，无业务逻辑）
├── demo.py            最小演示（无参跑两个例子 / 传参查一句）
├── demo_full.py       完整演示（五部分）
├── builders/          离线数据管道（不参与运行）
│   ├── build_kg.py        OpenCMKG → CSV
│   └── import_kg_nodes.py CSV + 排班表 → MySQL
├── .env               本模块配置（不入库）
├── .env.example       模板
├── fctest/            测试 + 示例（整目录删掉不影响网站）
│   ├── run_tests.py              125 个用例
│   ├── test_config.py            测试共享配置 + 路径
│   ├── test_data_integrity.py    CSV 产物完整性（28）
│   ├── test_kg_nodes.py          图数据健康度 + 端到端（23）
│   ├── test_kg_layer2.py         工具集/遍历/选择器（38）
│   ├── test_llm_config.py        LLM 配置中心（36）
│   ├── example_call.py           真实输入的调用返回
│   └── example_kgapi.py          kgapi 调用示例
└── dev-help/          完整文档 + 问题记录

调用方最少需要的文件：
    paths.py  db.py  store.py  agent.py  recommend.py  config.py  prompts.py
    + prompts/select_nodes.md  + .env
```

## 调用

```python
import sys; sys.path.insert(0, "tests/kg")
from agent import get_selector
from recommend import traverse

result = get_selector().select("腿疼")     # ① 文本 → 节点
deps = traverse(result["nodes"])           # ② 节点 → 科室
print(deps["departments"][0]["科室"])      # 骨科专科
```

Django 那边通过 `LLM_server/kg_bridge.py` 调同一套。

# 数据

| 维度 | 数量 |
|---|---|
| 节点 | 20,465 |
| 边 | 131,363 |
| 别名 | 10,895 |

三张表：`kg_node`（节点）、`kg_edge`（关系）、`kg_alias`（口语别名）。
`node_key` 格式 `类型:名称`，如 `symptom:腰痛`。

# 注意

- 输出只是**导诊参考**，不是诊断。
- 医学知识来自 OpenCMKG，**仅限学术研究，不得商用**。
- 改 `prompts/*.md` 立即生效；改科室映射/同义词要**重新导入**才生效。
