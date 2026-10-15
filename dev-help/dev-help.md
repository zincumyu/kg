# tests/kg —— 知识图谱模块 · 完整入门

> **一句话**：把患者的一句中文，变成「该挂哪个科」。
>
> ```
> 输入  腿疼
> 输出  骨科专科（置信度 0.33，依据：关节退行性变、胸腰椎骨折、腰椎病）
> ```

这个目录**不是测试目录**，而是知识图谱程序本身 —— 不依赖 Django，
用 pymysql 直接读 MySQL，可以整体拷走。

> ⚡ **只想先跑一下？** 跳到 [第 2 节](#2-5-分钟跑起来)。
> 📖 **想搞懂原理？** 从 [第 1 节](#1-这是什么) 顺序读。

---

## 目录

- [1. 这是什么](#1-这是什么)
- [2. 5 分钟跑起来](#2-5-分钟跑起来)
- [3. 它怎么工作](#3-它怎么工作)
- [4. 数据模型](#4-数据模型)
- [5. 怎么调用](#5-怎么调用)
- [6. 怎么改](#6-怎么改)
- [7. 测试](#7-测试)
- [8. 已知限制](#8-已知限制)
- [9. 目录结构](#9-目录结构)
- [10. 相关文档](#10-相关文档)

---

## 1. 这是什么

### 1.1 解决什么问题

患者描述症状时不会用医学术语：

| 患者说 | 医学术语 | 该挂的科 |
|---|---|---|
| 拉肚子 | 腹泻 / 水样便 | 消化专科 |
| 心口像压了块石头 | 胸闷 / 胸痛 | 呼吸内科 或 心血管专科 |
| 腿疼 | 腰痛 / 腰腿痛 | 骨科专科 |

医院挂号系统要求的是**标准科室名**，中间这层翻译就是本模块干的活。

### 1.2 在项目里的位置

```
浏览器
  │  表单 POST（question=我这两天肚子不舒服）
  ▼
Django  LLM_server/aichat/views.py
  │
  ▼
LangGraph 8 节点  LLM_server/aichat/medical_agent.py
  │
  │  其中「知识图谱推理」这个节点调用本模块
  ▼
LLM_server/kg_bridge.py          ← Django ↔ 本模块的唯一切入点
  │
  ▼
tests/kg/  ★ 本模块
  │
  ▼
MySQL  kg_node / kg_edge / kg_alias
```

**本模块只做一件事**：文本 → 科室。写成给患者看的话、拼挂号卡片、存聊天记录，
都是 Django 那边的事，不在这里。

### 1.3 能力边界

| ✅ 做 | ❌ 不做 |
|---|---|
| 症状/口语 → 标准节点 | 不下医学诊断 |
| 节点 → 候选科室 + 置信度 | 不生成自然语言回答 |
| 给出推理依据（哪些疾病投的票） | 不查医生排班（那是 Django 的 `HospitalSchedule`） |
| 规则层不命中时调 LLM 选节点 | 不做多轮问诊 |

**输出永远只是导诊参考，不是诊断结论。**

### 1.4 为什么不直接用 Neo4j

项目原设计是 `向量检索 → Neo4j Cypher 多跳 → 科室`，但：

| 问题 | 后果 |
|---|---|
| Neo4j 与 Milvus 都没部署 | 链路上的 `mixed_retriever` 退化成 `None` |
| 图谱里只有 6 条硬编码元组 | 就算服务起来了，查询结果也几乎为空 |

所以改成：**公开数据集 OpenCMKG 建图，落进网站已有的 MySQL**。

- 不引入新服务（MySQL 网站本来就要用，挂了反正整个站都挂）
- 实测工具层 8 ms / 遍历 27 ms，比远程图数据库更快
- 代价是失去 Cypher 的表达力 —— 但本项目只需要「多跳 + 投票」，SQL 够用

原 `mixed_retrieval.py` 的代码**保留没删**，等以后真要上 Neo4j 再接。

---

## 2. 5 分钟跑起来

### 2.1 前置条件

| 项 | 要求 |
|---|---|
| MySQL | 容器 `medical-mysql` 跑着，库 `ai250301` |
| 图谱数据 | `kg_node` / `kg_edge` / `kg_alias` 三张表有数据 |
| Python | 3.11+，已装 `pymysql` / `python-dotenv` |
| LLM API Key | **只有整句输入才需要**，短词走规则层不用 |

环境变量写在 `tests/kg/.env`（优先级最高），没写的会回落到 `LLM_server/.env`。

### 2.2 三条命令

```powershell
cd <项目根>
conda activate workflows_env    # 只需一次
$py = "python"

# ① 最小演示：两个输入，看调用长什么样（约 10 秒）
& $py tests/kg/demo.py

# ② 查一句话会被推荐到哪个科
& $py tests/kg/demo.py 腿疼

# ③ 跑全部测试（约 25 秒）
& $py tests/kg/fctest/run_tests.py
```

`demo.py` 应该输出：

```
================================================================
例 1 —— 规则层（别名表精确匹配，不调 LLM）
================================================================
输入 : 腿痛
  ①节点 : ['symptom:腿痛']
    来源 : rule      耗时 : 51 毫秒
  ②科室 : 风湿免疫科      置信度 1.00

================================================================
例 2 —— LLM 层（规则层没命中，调 LLM 选节点）
================================================================
输入 : 我这两天胸口发闷，喘不上气
  ①节点 : ['symptom:胸闷', 'symptom:呼吸困难', 'symptom:胸闷气短']
    来源 : llm      耗时 : 6886 毫秒
  ②科室 : 呼吸内科       置信度 0.18
  ②科室 : 心血管专科      置信度 0.16
```

### 2.3 如果数据库是空的

```
kg_node 表数据不足  →  按下面的顺序重建
```

```powershell
# 1) 从 OpenCMKG 数据集生成 CSV（约 5 秒）
& $py tests/kg/builders/build_kg.py --verify

# 2) 导入 MySQL（约 1 分钟，会清空重建三张表）
& $py tests/kg/builders/import_kg_nodes.py

# 只想看结果不想写库 → 加 --stats 空跑
```

### 2.4 其它常用命令

```powershell
& $py tests/kg/demo_full.py              # 完整演示（五部分，约 30 秒）
& $py tests/kg/demo_full.py --stats      # 只看数据规模
& $py tests/kg/config.py                 # 看当前 LLM 配置（Key 已脱敏）
& $py tests/kg/config.py --probe         # 探测网关支持哪些模型
& $py tests/kg/prompts.py                # 看当前提示词
& $py tests/kg/fctest/run_tests.py --perf  # 跑测试 + 性能基准
```

### 2.5 数据库怎么来的（`database/` 两个 SQL）

| 文件 | 大小 | 内容 |
|---|---|---|
| `database/medical.sql` | 2.2 MB | 22 张业务表（`aichat_*` / `auth_*` / `user_*` / 排班表…） |
| `database/kg_graph.sql` | 19 MB | KG 三张表 + 全部数据，**末尾手工补了 2 行迁移记录** |

两者都由 docker 首次启动时自动导入（`docker/mysql_docker/docker-compose.yml` 里的
`docker-entrypoint-initdb.d` 挂载）。**只在 `mysql-data` 为空时执行**，已有环境不会重跑。

#### 为什么 `medical.sql` 只有 2.2 MB

原始 dump 是 48 MB，其中 **95.5% 是 LangGraph 的对话状态快照**：

```
checkpoints            27.3 MB   58.7%
checkpoint_writes      11.8 MB   25.4%
checkpoint_blobs        5.3 MB   11.4%
──────────────────────────────────────
                        44.4 MB   95.5%   ← 全是对话记忆
aichat_chat             1.0 MB    2.2%   ← 真实业务数据
aichat_hospitalschedule 0.3 MB    0.7%
其余                    <0.4 MB
```

库里 1785 个会话，最大的一个 `thread_id=167` 存了 74 条快照。

这四张表**由 `medical_agent.py` 里的 `saver.setup()` 在启动时自动重建**，
dump 时排除掉，`medical.sql` 就从 48 MB 掉到 2.2 MB。

#### 为什么 `kg_graph.sql` 要补两行迁移记录

Django 的 `migrate` 靠 `django_migrations` 表判断哪个迁移跑过。
`kg_graph.sql` 自己建了 KG 三张表，但没告诉 Django，所以文件末尾必须手工加：

```sql
INSERT INTO `django_migrations` (`app`,`name`,`applied`) VALUES
  ('aichat','0015_kgalias_kgedge_kgnode', NOW()),
  ('aichat','0016_alter_kgalias_unique_together_and_more', NOW());
```

不补的话，别人跑 `manage.py migrate` 会以为 `0015` 没执行过，重复 `CREATE TABLE` 直接报错。

> ⚠️ `medical.sql` 里的 `django_migrations` 记录只到 `aichat/0014`，所以这两行必须由
> `kg_graph.sql` 提供。**顺序不能反** —— `kg_graph.sql` 要往 `django_migrations` 插数据，
> 那张表得先由 `medical.sql` 建出来。

#### 怎么重新生成

```powershell
# 业务表：排除 checkpoint*（会被 saver.setup() 重建）和 kg_*
docker exec medical-mysql mysqldump -uroot -p1111 --single-transaction --no-create-db ai250301 `
  --ignore-table=ai250301.checkpoints --ignore-table=ai250301.checkpoint_writes `
  --ignore-table=ai250301.checkpoint_blobs --ignore-table=ai250301.checkpoint_migrations `
  --ignore-table=ai250301.kg_node --ignore-table=ai250301.kg_edge --ignore-table=ai250301.kg_alias `
  > database/medical.sql

# KG 三张表
docker exec medical-mysql mysqldump -uroot -p1111 --single-transaction --no-create-db ai250301 `
  kg_node kg_edge kg_alias > database/kg_graph.sql
```

生成 `kg_graph.sql` 后**手工在末尾追加那两行 `INSERT INTO django_migrations`**（见上）。

改了图谱数据（重跑 importer / 改同义词表）后，要重新生成 `kg_graph.sql` 并一起提交。

---

## 3. 它怎么工作

### 3.1 三层架构

```
┌─ 第 1 层 ─────────────────────────────────────────────┐
│  节点存储      MySQL：kg_node / kg_edge / kg_alias     │
│                20,465 节点 / 131,363 边 / 10,895 别名  │
└───────────────────────────────────────────────────────┘
                        ▲
┌─ 第 2 层 ─────────────┴───────────────────────────────┐
│  LLM 操作节点   store.py 提供 4 个**只读**工具：        │
│                   search_nodes / get_node             │
│                   expand_node  / find_path            │
│                 agent.py 用这些工具把文本变成节点       │
│                 LLM 只能「选」，不能「造」              │
└───────────────────────────────────────────────────────┘
                        ▲
┌─ 第 3 层 ─────────────┴───────────────────────────────┐
│  文本 → 节点     recommend.py 从节点遍历加权投票出科室   │
└───────────────────────────────────────────────────────┘
```

### 3.2 文本 → 节点 → 科室

以「腿疼」为例：

```
「腿疼」
   │
   │ ① 查 kg_alias（口语 → 节点）
   ▼
腰腿痛[disease] 、 腰痛[symptom]
   │
   ├─ 起点是 symptom ──────────────────────────────┐
   │    ② 反向遍历「表现为」                        │
   │    symptom:腰痛 ← disease:X （14 个疾病）      │
   │    ③ 每个疾病遍历「就诊于」                     │
   │    → 骨科专科(5票) 泌尿专科(3票) 妇科专科(2票)  │
   │                                               │
   └─ 起点是 disease ──────────────────────────────┤
        ② 直接遍历「就诊于」                        │
        disease:腰腿痛 → 外科普诊(1票)              │
                                                   │
   ┌──────────────────────────────────────────────┘
   │ ④ 合并加权排序
   ▼
骨科专科 (0.33) > 泌尿专科 (0.20) > 妇科专科 (0.13)
   │
   ▼
✅ 推荐挂号：骨科专科
```

**置信度 = 该科室得票 ÷ 总票数**，是**相对比例**，不是准确率。
0.33 不代表「33% 正确」，只代表「在候选里它得票占三分之一」。

### 3.3 规则层 vs LLM 层（成本差 500 倍）

`agent.py` 的 `select()` 是**两级降级**：

| 级别 | 触发条件 | 耗时 | 成本 |
|---|---|---|---|
| ① 规则层 | 口语在 `kg_alias` 里精确命中 | **17 ms** | 0 |
| ② LLM 层 | 规则层没命中 | **9~10 秒** | 消耗 API 额度 |
| ③ 都没有 | LLM 也没找到 | — | 答「不确定」 |

关键代码：

```python
def select(self, text, use_llm=True):
    keys = self._rule_match(text)      # ① 规则层永远先跑
    if keys:
        return {...}                   # 命中就直接返回，不碰 LLM
    if use_llm:                        # ② 没命中才调 LLM
        keys, trace, note = self._llm_select(text)
```

所以**短词永远不会调 LLM**，即使 `use_llm=True`。
这也意味着：**每往别名表里加一个词，就有一批问句从 10 秒降到 17 毫秒。**

### 3.4 防幻觉：LLM 只能选，不能造

这是整个设计里最重要的一条。LLM 拿到的是 `search_nodes` **真实返回过**的候选，
提交时必须经过图谱校验：

```python
valid, invalid = self.store.validate_keys(submitted)
# invalid 里的 node_key 会被直接丢掉
```

`trace` 里能看到这个过程：

```json
{
  "round": 4, "tool": "select_nodes",
  "submitted": ["symptom:腹痛", "symptom:恶心"],
  "valid":     ["symptom:腹痛", "symptom:恶心"],
  "invalid":   [],
  "reason":    "患者主诉「肚子不舒服」对应腹部症状节点..."
}
```

**即使 LLM 编造了一个不存在的科室，也传不到下游。**

### 3.5 两个踩过的坑

**坑 1：疾病起点不要做症状扩散**

曾经试过「疾病 → 它的症状 → 那些症状关联的其他疾病 → 科室」来扩大召回，**实测有害**：

```
腰腿痛(疾病) 直接就诊于 → 外科普诊         1 票，正确
扩散后污染成 191 票，把「外科普诊」顶到第一名，
反而压掉了更合理的「骨科专科」
```

医疗场景**精度优先于召回**，所以疾病起点只走直接边。

**坑 2：别名优先级高于节点名匹配**

`store.search_nodes` 的优先级是：

```
alias(0) > name_exact(1) > alias_stripped(0.5) > name_partial(2)
```

所以给一个**本身就是节点名**的词加别名，会把它抢到别的节点去：

```
「心慌」本来精确匹配到节点 symptom:心慌
若给它加别名「心慌 → 心悸」，患者说「心慌」就会返回「心悸」  ❌ 倒退
```

导入时已自动跳过这类（实测 16 条），见 [6.3](#63-加同义词)。

---

## 4. 数据模型

### 4.1 三张表

```
kg_node    节点表
  node_key    "类型:名称"  唯一   如 symptom:下肢疼痛
  node_type   symptom / disease / department / check_item / doctor
  name        规范名称
  extra       JSON，类型特有属性
  source      opencmkg / hospital

kg_edge    关系表
  src_key → dst_key
  relation    表现为 / 就诊于 / 需要检查 / 并发症 / 所属科室
  UNIQUE(src_key, dst_key, relation)

kg_alias   别名表
  alias      口语表达
  node_key   指向的规范节点
  source     opencmkg / manual
  UNIQUE(alias, node_key)     允许一个口语对应多个概念
```

> ⚠️ 这三张表**不是 Django 模型**（故意的，这样本模块才能独立运行）。
> 但**表是 Django 迁移建的** —— `aichat/migrations/0015` 真的建表，
> `0016` 只把模型移出 Django state（`database_operations=[]`），不删表。
> **不要删除这两个迁移**，否则全新数据库跑 `migrate` 建不出表。

### 4.2 关系方向（最容易搞错）

```
疾病 ──表现为──▶ 症状      查「某症状关联哪些疾病」→ 查 dst_key 反向
疾病 ──就诊于──▶ 科室      查「某疾病去哪个科」    → 查 src_key 正向
疾病 ──需要检查──▶ 检查项
疾病 ──并发症──▶ 疾病
医生 ──所属科室──▶ 科室
```

查「腿疼关联哪些疾病」：

```python
import sys; sys.path.insert(0, "tests/kg")
import kgapi

for key in kgapi.alias_targets("腿疼"):        # → ['disease:腰腿痛', 'symptom:腰痛']
    if key.startswith("symptom:"):
        # 症状要**反向**查「表现为」的边
        for e in kgapi.in_edges(key, relation="表现为"):
            print(e["src_key"])                # 疾病节点
    else:
        # 疾病直接正向查「就诊于」
        for e in kgapi.out_edges(key, relation="就诊于"):
            print(e["dst_key"])                # 科室节点
```

### 4.3 当前数据规模

| 维度 | 数量 |
|---|---|
| 节点 | 20,465 |
| ├ 疾病 | 8,986 |
| ├ 症状 | 6,488 |
| ├ 检查项 | 4,146 |
| ├ 医生 | 590 |
| └ 科室 | 255（**全部可挂号**） |
| 边 | 131,363 |
| 别名 | 10,895（覆盖 10,213 个口语表达，其中 36 条来自手工表） |

### 4.4 ⚠️ 647 个「同名双类型」节点

`腰痛`、`胸痛`、`血尿` 等 647 个名字**同时存在** `symptom:腰痛` 和 `disease:腰痛` 两个节点。

**别名导入时已处理**：同名时选「边更多」的那个（边多说明它在图里真被使用）。

```
腰痛  : symptom(14 条边)  vs  disease(2 条)   → 选 symptom ✅
腰腿痛: disease(21 条边)  vs  symptom(不存在)  → 选 disease ✅
```

**但你直接调 `traverse()` 时，起点可能是症状也可能是疾病，必须两种情况都处理**
（`recommend.py` 就是这么写的：症状走反向边 + 疾病走正向边，然后合并投票）。

### 4.5 想自己查数据：用 `kgapi`

`kgapi.py` 把三张表包成只读函数，**不依赖 Django**，查数/写脚本/导报表都方便。

```powershell
& $py tests/kg/fctest/example_kgapi.py     # 跑一遍看全部用法（有真实输出）
```

```python
import kgapi

kgapi.counts()                                     # {'kg_node': 20465, ...}
kgapi.get_node("symptom:腰痛")                      # → dict，没有则 None
kgapi.get_nodes_by_name("腰痛")                     # → 同名双类型都拿到
kgapi.alias_names("腿疼")                           # → ['腰腿痛', '腰痛']
kgapi.out_edges("disease:腰椎间盘突出", "就诊于")     # → 这个病挂哪个科
kgapi.in_edges("symptom:腰痛", "表现为")             # → 这个症状关联哪些病
kgapi.department_names()                           # → 全部科室名
```

| 分组 | 函数 |
|---|---|
| 统计 | `counts` `node_count` `edge_count` `alias_count` `node_types` |
| 节点 | `get_node` `node_exists` `get_nodes_by_name` `all_nodes` |
| 别名 | `alias_targets` `alias_names` `alias_target_counts` `all_aliases_row` |
| 边 | `in_edges` `out_edges` |
| 科室 | `department_names` `pediatric_departments` |

> ⚠️ `kgapi` 是**简单直查，没有业务逻辑**（不投票、不过滤儿科、不判置信度）。
> **生产代码不要用它** —— 生产走 `store.py` + `recommend.py`。
>
> 要写自己的 SQL 就用更底层的 `db`：
> ```python
> import db
> db.query("SELECT ...")            # → list[dict]
> db.query_one("SELECT ...")        # → dict 或 None
> ```

---

## 5. 怎么调用

### 5.1 就两步

```python
import sys; sys.path.insert(0, "tests/kg")
from agent import get_selector
from recommend import traverse

result = get_selector().select("腿疼")     # ① 文本 → 节点
deps = traverse(result["nodes"])           # ② 节点 → 科室

print(deps["departments"][0]["科室"])      # 骨科专科
```

**进去是普通字符串，出来是 Python dict，全程不涉及 JSON。**

### 5.2 `select()` 的返回

```json
{
  "nodes": ["disease:腰腿痛", "symptom:腰痛"],
  "source": "rule",
  "trace": [{"stage": "rule", "hit": ["disease:腰腿痛", "symptom:腰痛"]}],
  "elapsed_ms": 58,
  "note": ""
}
```

| 字段 | 说明 |
|---|---|
| `nodes` | 选中的节点（已通过图谱校验，一定是真实存在的） |
| `source` | `rule` / `llm` / `none` —— 走的哪一级降级 |
| `trace` | 审计线索。`source=llm` 时含每一轮工具调用和 LLM 的选词理由 |
| `elapsed_ms` | 耗时。`rule` 约 20ms，`llm` 约 10000ms |
| `note` | 走不通时的原因，如「调用方要求跳过 LLM」 |

### 5.3 `traverse()` 的返回

```json
{
  "departments": [
    {"科室": "骨科专科", "科室键": "department:骨科专科",
     "票数": 5, "置信度": 0.3333,
     "依据": ["关节退行性变", "胸腰椎骨折", "腰椎病"]}
  ],
  "total_votes": 15,
  "confidence": 0.3333,
  "enough": true,
  "filtered_pediatric": ["儿科内科", "小儿外科专科"]
}
```

| 字段 | 说明 |
|---|---|
| `departments` | 已排序的候选科室，含票数、置信度、**投票依据** |
| `confidence` | 第一名的置信度 |
| `enough` | 是否达到 `KG_MIN_CONFIDENCE`（默认 0.15）。**false 时上层应该拒答** |
| `filtered_pediatric` | 被年龄过滤掉的儿科科室（没传 `age` 时，儿科一律排除） |

参数：`traverse(nodes, top_n=3, min_confidence=None, age=None)`

### 5.4 网站（Django）怎么调

`medical_agent.py` 走的是同一套：

```python
from kg_bridge import get_selector, traverse, reason_text

sel = get_selector().select(processed_input)      # 没传 use_llm → 默认 True
r = traverse(sel["nodes"], top_n=3, age=age)
```

`kg_bridge.py` 只做路径处理，把 `tests/kg` 加进 `sys.path` 再转发。
这样业务代码里写的是干净的 `from kg_bridge import ...`，
以后本模块换位置只改那一个文件。

**注意 `kg_bridge` 没传 `use_llm`，所以网站默认允许调 LLM。**
`demo.py` 则显式传了 `use_llm=False`，方便不花额度地批量试。

### 5.5 示例文件速查

| 文件 | 看什么 |
|---|---|
| `demo.py` | **最小演示**：两个输入，逐步打印 |
| `demo.py` | 最小调用（50 行） |
| `fctest/example_call.py` | 真实输入时**每一步的原始返回**（写代码前先看这个） |
| `fctest/example_kgapi.py` | 用 kgapi 查数据 |
| `demo_full.py` | 完整演示（五部分） |

---

## 6. 怎么改

### 6.1 改完必须做的四件事

无论改什么，都走这个流程：

```powershell
cd <项目根>
conda activate workflows_env    # 只需一次
$PY = "python"

& $PY tests/kg/builders/build_kg.py --verify          # 1) 重建 CSV（约 5 秒）
& $PY tests/kg/builders/import_kg_nodes.py --stats    # 2) 空跑，看有没有死链/冲突
& $PY tests/kg/builders/import_kg_nodes.py            # 3) 真导入（约 1 分钟）
& $PY tests/kg/fctest/run_tests.py                    # 4) 跑测试（约 25 秒）
```

**第 4 步必须全绿**才能提交。

> ⚠️ 只有改 `prompts/*.md` 不需要重新导入 —— 提示词是**运行时**读的。
> 其余（科室映射、同义词）都是**构建时**读的，改完必须重新导入才生效。

### 6.2 改科室映射

位置：`builders/build_kg.py` 的 `DEPARTMENT_MAP`

```python
"营养科":   ["健康体检"],          # 医院无独立营养科
"急诊科":   ["全科医学专科"],      # 医院无急诊挂号
```

**必须在注释里写明这是降级映射**，方便后续核对。

改完跑 `run_tests.py integrity -v`，重点看 `test_all_recommended_depts_bookable`
（保证推荐出来的科室患者真能挂到号）是否通过。

> ⚠️ 这 48 条映射目前是**靠名称相似度手写的，未经医学人员核对**，
> 是当前**最大的准确率风险**。

### 6.3 加同义词

位置：`data/kg/symptom_synonyms.csv`

```csv
口语表达,规范术语,备注
手腕疼,腕关节疼痛,
```

| 列 | 要求 |
|---|---|
| 口语表达 | 患者会说的词，**同一列不能重复**（有测试强制校验） |
| 规范术语 | 必须是 `kg_node` 里**真实存在的节点名**，否则是死链 |

导入时会打印四类统计，**请逐条核对**：

```
手工同义词 89 条（89 个口语表达）→ 新增 36，跳过(别名本身是节点名) 16，已有 37，死链 0
    ⚠️  与自动管道冲突 9 条（手工表覆盖，原指向已丢弃）:
        气促  自动→symptom:手足多汗  手工→symptom:气短
        脸肿  自动→symptom:眼胀      手工→symptom:面部水肿
```

- **死链必须为 0**
- **冲突**是正常的（手工表判断优先于自动推断），但要逐条核对。上例两条都是自动管道错了
- **跳过（别名本身是节点名）**：说明这个词已经是图谱里的节点，靠 `name_exact` 就命中了，
  加别名反而会把它抢到别的节点去（见 [3.5 坑 2](#35-两个踩过的坑)）

#### ⚠️ 陷阱：语义对 ≠ 推荐对

手工表**优先级高于**自动管道，写一行就会丢弃管道原本的结果。
如果目标节点在图里几乎没有连线，它会**把一个本来正确的推荐改坏**。

真实案例（已修正，该行已删除）：

| | 别名指向 | 最终推荐 |
|---|---|---|
| 自动管道 | `disease:腰腿痛` + `symptom:腰痛` | 骨科专科 ✅ |
| 手工写成 `腿疼 → 腿痛` | `symptom:腿痛` | ❌ **风湿免疫科（置信度 1.0）** |

`腿疼 = 腿痛` 语言上没错，但 `symptom:腿痛` 只连着 **1 个疾病**，
而 `symptom:腰痛` 连着 12 个。结果推荐错科，**置信度反而从 0.33 涨到 1.00** ——
只有一个候选，投票必然全票。

**写之前先查目标节点有多少条边：**

```powershell
& $PY -c 'import sys; sys.path.insert(0, "tests/kg")
import db
name = "腿痛"                      # ← 改成你要指向的规范术语
r = db.query_one("SELECT node_key FROM kg_node WHERE name=%s LIMIT 1", (name,))
if not r: print("❌ 该节点不存在"); raise SystemExit
k = r["node_key"]
n = db.query_one("SELECT COUNT(*) c FROM kg_edge WHERE dst_key=%s", (k,))["c"]
print("%s -> %s，入边 %d 条" % (name, k, n))
if n < 3: print("⚠️ 只有 %d 条边，用它做别名目标很可能推荐不出正确科室" % n)
'
```

**经验法则：入边少于 3 条的节点，不要拿来当手工别名的目标。**

### 6.4 改提示词

**提示词不写在代码里**，放在 `tests/kg/prompts/`：

```
prompts/select_nodes.md     节点选择器的 system prompt
```

改 `.md` 文件即可，**不用改代码、不用重启**（每次调用都重新读，无缓存）。

```powershell
& $PY tests/kg/prompts.py         # 看当前有哪些提示词
```

为什么用 `.md` 文件而不是 `.env` 或数据库：

| 方案 | 问题 |
|---|---|
| 写死在 `.py` 里 | 改提示词要动代码，非开发人员改不了 |
| 塞进 `.env` | 多行长文本要转义换行，diff 没法看 |
| 存数据库 | 没法跟着代码分支/回滚，提示词变更需要**版本可追溯** |

`prompts.py` 在文件不存在时**故意抛异常**而不是回落到代码里的默认值 ——
如果回落，就会出现「两份提示词，改了一份另一份不生效」，这类问题极难排查。

### 6.5 调置信度阈值

位置：`recommend.py`（约第 66 行），**改配置不改代码**：

```python
MIN_CONFIDENCE = _env_float("KG_MIN_CONFIDENCE", 0.15)
```

在 `tests/kg/.env` 里覆盖：

```ini
KG_MIN_CONFIDENCE=0.20
KG_PEDIATRIC_MAX_AGE=14    # 小于该年龄才允许推荐儿科；不填年龄则一律不推儿科
```

**怎么标定**：只用规则层能命中的短词，不要拿整句去跑（切词会给出错误结论）。

实测分布（2026-10 数据）：

| 输入 | 推荐科室 | 置信度 |
|---|---|---|
| 牙疼 | 口腔内科 | 0.774 |
| 眼睛疼 | 眼科专科 | 0.759 |
| 咳嗽 | 呼吸内科 | 0.373 |
| 腰痛 | 骨科专科 | 0.357 |
| 腿疼 | 骨科专科 | 0.333 |
| 拉肚子 | 消化专科 | 0.243 |
| 胸闷 | 呼吸内科 | 0.214 |
| 头晕 | 心血管专科 | 0.157 |
| 失眠 | 心理卫生专科 | 0.132 |
| 发烧 | 感染疾病科 | 0.104 |

默认 **0.15** 正好落在分界附近：调高会让「发烧」「失眠」直接答不确定，
调低会让低置信结果也照常推荐。

> ⚠️ 本项目的置信度是**相对投票比例**，与申请书里的「阈值 0.85」**量纲不同**，
> 不能直接套用。

---

## 7. 测试

### 7.1 运行方式

```powershell
& $PY tests/kg/fctest/run_tests.py              # 全部（约 25 秒）
& $PY tests/kg/fctest/run_tests.py -v           # 显示每个测试方法
& $PY tests/kg/fctest/run_tests.py -q           # 只显示统计
& $PY tests/kg/fctest/run_tests.py nodes        # 只跑某个测试集
& $PY tests/kg/fctest/run_tests.py --perf       # 附带性能基准
& $PY tests/kg/fctest/run_tests.py layer2 --live  # 允许调真实 LLM
```

退出码：`0` 全通过 / `1` 有失败 / `2` 环境不满足。可直接用于 CI。

其它运行方式：

```powershell
& $PY tests/kg/fctest/test_kg_nodes.py                           # 单个文件
& $PY -m unittest tests.kg.fctest.test_kg_nodes.TestGraphTraversal -v
& $PY -m unittest discover -s tests/kg/fctest -t tests/kg/fctest -v
```

### 7.2 各测试集分工

| 测试集 | 文件 | 用例 | 测什么 | 依赖 |
|---|---|---|---|---|
| `integrity` | `test_data_integrity.py` | 28 | CSV 产物是否健康 | 无 |
| `nodes` | `test_kg_nodes.py` | 23 | MySQL 节点层 + 端到端 | MySQL |
| `layer2` | `test_kg_layer2.py` | 44 | 工具集 / 遍历投票 / 节点选择器 | MySQL（LLM 用例需 `--live`） |
| `llm` | `test_llm_config.py` | 36 | LLM 配置中心（优先级、降级、校验） | 无 |

共 **125 个用例**。`test_config.py` 是共用的路径与数据加载。

### 7.3 重点用例（这几条最值钱）

| 用例 | 验证什么 | 为什么重要 |
|---|---|---|
| `test_all_recommended_depts_bookable` | 推荐科室必须能在排班表查到 | **否则患者挂不到号** |
| `test_traverse_top_department_correct` | 医学常识校验（腿疼→骨科） | 端到端正确性 |
| `test_true_negation_filtered` | 「腰没有疼」必须被拒绝 | 防反向误推荐 |
| `test_cypher_is_idempotent` | 导入脚本不得含 `DETACH DELETE` | 防清空数据库 |
| `test_edge_endpoints_exist` | 边两端必须指向存在的节点 | 引用完整性 |
| `test_link_fanout_reasonable` | 单个口语指向 ≤5 个节点 | 防过度泛化 |
| `test_most_aliases_have_single_target` | 单目标别名占比 ≥90% | 歧义度可控 |
| `test_repeated_calls_identical` | 同输入结果必须一致 | 防随机性，保证审计可复现 |
| `test_validate_keys_filters_fake` | 能识别 LLM 编造的假节点 | **防幻觉的核心机制** |
| `test_pediatric_filter_adult` | 成年人不被推荐儿科 | 实测踩过的坑 |

> ⚠️ **写 LLM 相关的断言时不要卡死具体节点。**
> 实测同一句「我这两天胸口发闷，喘不上气」跑两次，选出的节点和第一名科室都会变
> （呼吸内科 0.19 ↔ 心血管专科 0.23）。只能断言「属于合理集合」。

### 7.4 测试失败怎么办

| 报错 | 原因 | 处理 |
|---|---|---|
| `kg_node 表数据不足` | 数据库空 | 跑 [2.3](#23-如果数据库是空的) 的重建流程 |
| `无法初始化 Django` | `nodes` 测试要连 MySQL | 确认容器在跑；纯离线测可只跑 `integrity` |
| `nodes` 测试全部 skip | 连不上 MySQL | 同上 |
| 端到端推荐结果错误 | 别名指错 / 映射错 | 查 [6.3](#63-加同义词) 的冲突日志 |
| 数据规模测试失败 | 数据集换了 | 调整 `test_data_integrity.py` 的 `EXPECTED` 区间 |

**排查单个输入的正确姿势**（不要拿整句直接喂 `traverse`）：

```powershell
& $PY -c 'import sys; sys.path.insert(0, "tests/kg")
from agent import get_selector
from store import get_store
from recommend import traverse

t = "患者说的一句话"     # ← 改这里
sel = get_selector()
print("1. 规则层命中 :", sel._rule_match(t))
print("2. 候选节点   :", [(n["node_key"], n["match"]) for n in get_store().search_nodes(t, limit=5)["nodes"]])
r = traverse(sel._rule_match(t) or ["symptom:腰痛"], top_n=3, min_confidence=0)
print("3. 推荐结果   :", [(x["科室"], x["置信度"]) for x in r["departments"]])
'
```

⚠️ **不要拿整句去跑 `store.search_nodes` 再喂给 `traverse`** ——
实测「肚子疼拉肚子」会被切词切成 `孕妇肚子痒`，一路推成「产科 1.0」，结论完全错误。
从整句到节点是 **LLM 选节点层**的职责，不是切词的职责。

---

## 8. 已知限制

以下都是**实测确认**的限制，不是理论问题。

| # | 限制 | 影响 | 解决方向 |
|---|---|---|---|
| 1 | **只有 37 个科室能从疾病到达** | 图里有 255 个科室节点（全部可挂号），但只有 37 个有「就诊于」入边；医院实际有 **284** 个可挂号科室 | 推荐必然只落在这 37 个内；补 `DEPARTMENT_MAP` 可扩 |
| 2 | 科室映射未人工核对 | 48 条映射靠名称相似度推断 | **当前最大风险**，需医学人员核对 |
| 3 | 部分口语无命中 | 「手臂疼痛」「手心潮热」拿不到推荐 | 从 4,221 条真实对话挖掘补充 |
| 4 | 否定识别只在别名层 | 「我不头痛，就是有点晕」会同时匹配「头痛」和「头晕」 | 需在文本匹配层加否定检测 |
| 5 | 置信度是**相对值** | 0.33 是「得票/总票」，不是准确率 | 与申请书「阈值 0.85」量纲不同，**不能混用** |
| 6 | 数据集本身有噪声 | 少量别名语义相反（如「小便发白」归到「尿黄」簇） | 加权投票吸收；人工审核可提纯 |
| 7 | 6,818 条别名被丢弃 | 死胡同簇无有效桥接目标 | 需人工补充桥接表 |
| 8 | 962 个孤立节点 | 没有任何边的节点 | 属数据集边界 |
| 9 | 别名里有 8 条省略号噪声 | 如 `……` → `symptom:耳鸣` | 目前无害（用户不会输入省略号），可清 |
| 10 | LLM 结果不稳定 | 同一句话两次跑选出的节点不同 | 断言只能卡「合理集合」，不能卡死 |

### ⚠️ 数据许可

医学知识来自 **OpenCMKG**（<https://github.com/RuiqingDing/OpenCMKG>），
采用**仅限学术研究**许可，**不得商用**。

论文、报告、答辩材料中必须声明数据来源。详见 `data/opencmkg/数据来源与许可.md`。

### ⚠️ 医疗免责

本知识图谱用于**导诊参考**，输出结果**不构成医疗诊断建议**，
不得作为临床决策的唯一依据。

---

## 9. 目录结构

```
tests/kg/
│
│  ███ 知识图谱程序（不依赖 Django）███
├── db.py                      MySQL 数据访问层（pymysql）
├── store.py               ★   4 个只读工具（找节点/读/展开/找路径）
├── agent.py               ★   节点选择器（规则层 + LLM 工具调用）
├── recommend.py           ★   图遍历 + 加权投票 → 科室
├── config.py                  LLM 配置中心（读 .env）
├── prompts.py                 提示词加载器
├── prompts/               ★   system prompt 放这里（改完不用重启）
│   └── select_nodes.md
├── kgapi.py                   只读查询辅助（查三张表用）
├── demo.py                ★   最小演示（两个例子，约 10 秒）
├── demo_full.py           ★   完整演示（五部分，约 30 秒）
├── builders/                  离线数据管道
│   ├── build_kg.py            从 OpenCMKG 生成 CSV（含 DEPARTMENT_MAP）
│   └── import_kg_nodes.py     ETL 导入 MySQL
├── .env                       本模块配置（优先级最高，不入库）
├── .env.example               配置模板
│
│  ███ 测试 + 示例（整个目录删掉不影响网站）███
└── fctest/
    ├── run_tests.py           测试运行器（125 个用例）
    ├── test_config.py         测试共享配置 + 路径
    ├── test_data_integrity.py CSV 产物完整性（28）
    ├── test_kg_nodes.py       图数据健康度 + 端到端（23）
    ├── test_kg_layer2.py      工具集/遍历/选择器（38）
    ├── test_llm_config.py     LLM 配置中心（36）
    │
    ├── example_call.py        真实输入的调用返回
    └── example_kgapi.py       kgapi 调用示例
```

### 调用方最少需要哪些文件

```
kg_bridge.py（Django 侧）或直接 import
     └─> agent.py ──> config.py（LLM 客户端）
              └────> store.py ──> db.py ──> MySQL
         recommend.py ──────────┘
         prompts.py ──> prompts/select_nodes.md
```

**5 个程序文件 + `.env` + `prompts/`**：`db.py` `store.py` `agent.py` `recommend.py` `config.py`
+ `prompts.py`。其余（`builders/`、`demo*.py`、`kgapi.py`、`fctest/`）都不参与运行。

### 为什么不用 Django ORM

* 本模块要「不依赖 Django、能整体拷走」，用 ORM 就绑死了
* 这层只做只读查询 + 批量导入，ORM 没收益，反而让 ETL 变慢

代价是失去 Django 的 schema 检查 —— 由 `test_kg_nodes.py` 的完整性用例补上。

---

## 10. 相关文档

| 文档 | 内容 |
|---|---|
| `tests/kg/dev-help.md` | **本文** —— 完整文档（架构/数据模型/调用方式/怎么改/限制） |
| `tests/kg/README.md` | 快速上手（待补） |
| `docs/知识图谱技术设计.md` | 设计取舍、方案对比、性能特征 |
| `docs/知识图谱维护指南.md` | 给维护者的操作手册（改科室映射/补同义词的场景化步骤） |
| `dev_h/11_节点化改造方案.md` | 三层架构的设计方案 |
| `dev_h/09_知识图谱改造完成.md` | ⚠️ 已过时，第一阶段（内存版）的历史记录 |
| `data/opencmkg/数据来源与许可.md` | 数据来源与许可 |

**遇到问题先看**：[8. 已知限制](#8-已知限制) → [7.4 测试失败怎么办](#74-测试失败怎么办)。
