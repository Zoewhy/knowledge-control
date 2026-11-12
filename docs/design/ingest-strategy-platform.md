
# 知识入库策略配置与调优实验平台 · 技术方案

状态：待 review（P0 未开工）
分支：feature/multimodal-ingestion（后续独立分支开发）

## 0. 定位

把入库管线的硬编码决策抽象为**可配置策略**，配套**问答集导入**与**多策略对比实验**，
形成"换策略 → 重处理 → 评测 → 对比"闭环。表格类知识引入"向量路由 + 结构化查询"
双形态存储，补全检索链路的第四种模态。

- P0：策略层 + 第一档切分（heading/fixed/recursive/page + table_row）+ 重处理引擎
  + 问答集导入 + 手动多策略对比矩阵
- P1：clause / qa_pair 切分、grid 自动扫描、表格 structured 模式（DuckDB + Text-to-SQL）
- P2：parent_child / semantic 切分（需检索侧改造）、LLM 诊断式调参、达标自动判定

## 1. 总体架构

```
┌─ 配置面 ───────────────────────────────────────────────┐
│ IngestPolicy（策略实体，DB 持久化）                      │
│ EvalSet（问答集，复用 EvaluationCase + eval_group 分组）  │
└────────────────────────────────────────────────────────┘
┌─ 执行面 ───────────────────────────────────────────────┐
│ resolve_policy(document) → 状态机各步读策略决策           │
│ ReprocessEngine：从 DocumentFile 原文按新策略重跑         │
│   （claim/embedding 按内容 hash 缓存，重切分≠重抽取）     │
│ TableStore：TableBlock → DuckDB 物化 + table_card 段     │
└────────────────────────────────────────────────────────┘
┌─ 实验面 ───────────────────────────────────────────────┐
│ StrategyExperiment：文档 × 策略集 × 问答集               │
│   → 逐策略重处理（隔离副本）→ 逐策略评测 → 对比矩阵       │
└────────────────────────────────────────────────────────┘
```

## 2. 数据模型（迁移 0007）

### 2.1 `ingest_policies`

| 字段                              | 类型 | 说明                                                         |
| --------------------------------- | ---- | ------------------------------------------------------------ |
| id / name                         | str  | 如 "default"、"法规-条款切分"                                |
| doc_pattern                       | str  | `"*"` / `"pdf"` / `"source_type:feishu"` / 文件名 glob |
| parser_strategy                   | str  | `auto`（现状）/ `text_only`（跳过图片 VL）               |
| chunk_strategy                    | str  | chunkers registry 的 key，见 §3                             |
| chunk_params                      | JSON | 策略专属参数，CRUD 时按对应 pydantic schema 校验             |
| table_mode                        | str  | `chunk`（现状）/ `structured` / `hybrid`               |
| min_text_chars                    | int  | 默认 100                                                     |
| auto_compile                      | bool | 默认 true                                                    |
| version / created_at / updated_at |      |                                                              |

内置只读 `default` 策略，参数 = 现状全部常量，未配置时行为零变化。

### 2.2 `strategy_experiments` / `strategy_experiment_runs`

```
StrategyExperiment
├── name / document_ids[] / eval_group / target_metrics(JSON, P2 用)
└── runs[]: StrategyExperimentRun
     ├── policy_snapshot(JSON)     # 快照非引用，保证实验可复现
     ├── status: pending → reprocessing → evaluating → done / failed
     ├── metrics(JSON)             # EvaluationRun 同款聚合 + by_type 分层
     └── eval_run_id → evaluation_runs
```

### 2.3 其他

- `EvaluationCase` 加 `eval_group`（默认 "default"），实验按组跑
- `knowledge_tables`（P1）：DuckDB 表注册表（table_name / source_document_id /
  schema_json / row_count / content_hash / status）

## 3. 切分策略菜单

统一接口，新增策略 = 注册一个函数：

```python
ChunkerFn = Callable[[list[Block], ChunkParams], list[SegmentDraft]]
CHUNKERS: dict[str, ChunkerFn] = {...}   # IngestPolicy.chunk_strategy 即 key
```

| 策略             | 逻辑                               | 适用                 | 档位 |
| ---------------- | ---------------------------------- | -------------------- | ---- |
| `heading`      | 按标题层级，超长递归（现状）       | Markdown/SOP/飞书    | P0   |
| `fixed`        | 固定 token + overlap               | 无结构文本、OCR      | P0   |
| `recursive`    | 递归分隔符（段落→句子）保语义边界 | 报告、复盘           | P0   |
| `page`         | PDF 按页成块（版式即语义单元）     | 财报、合同扫描件     | P0   |
| `table_row`    | 大表按行成段，每段注入表头上下文   | 千行级配置表         | P0   |
| `clause`       | 法规按"第X条/（一）"正则切         | 法规、制度           | P1   |
| `qa_pair`      | 问答对整块不拆                     | FAQ、题库            | P1   |
| `parent_child` | 子块进索引、父块供生成上下文       | 长文档（需检索配合） | P2   |
| `semantic`     | 按相邻段落 embedding 相似度断点    | 主题转折多的文档     | P2   |

## 4. 表格双形态存储（P1）

```
TableBlock（headers + rows，解析层已产出）
  └─ table_mode = structured 时：
     ① 物化 DuckDB 表 kb_table_<hash>（嵌入式 OLAP，不污染 PG schema；
        schema 从 headers + 首行采样推断）
     ② 生成 table_card 段（segment_type="table_card"）：
        "表《类目保证金》：字段 类目/保证金/佣金率，共 N 行，类目含 …"
        —— 只有它进向量库/ES，rows 本体不嵌入
     ③ 注册 knowledge_tables

查询侧：
  Query Dispatcher 或 table_card 自然召回 → Agent 工具 structured_query
  （Tool Registry 注册，中风险）：
    LLM 生成 SQL → AST 白名单校验（仅 SELECT、表必须在 knowledge_tables 注册、
    强制 LIMIT、超时、结果行数上限）→ DuckDB 执行 → 结果集进答案上下文，
    citation 标注来源表
```

解决纯向量检索不可解的问题：聚合（"保证金>5000 的类目有几个"）、
排序（"哪个类目最高"）、多列联合过滤。P0 限定单表，join 留 P2。
mock 模式：structured_query 退化为模板查询，保证测试确定性。

## 5. 重处理引擎

```
reprocess(db, document_ids, policy_snapshot) -> None
├── 从 DocumentFile 读原始二进制（原文落库设计的回报）
├── 实验命名空间重建副本：SourceDocument/segment/claim
│   metadata_json["experiment_run_id"] 打标隔离
├── claim 缓存：statement fingerprint 命中全库已有 claim → 复用，
│   跳过 LLM 抽取（内容相同则抽取结果确定相同）
└── embedding 同理按文本 hash 缓存
```

隔离选 A：同库副本打标，检索/评测按 experiment_run_id 过滤。
评测入口强制带过滤参数，防止实验副本污染主库答案。
实验副本可保留复查或一键清理；P1 考虑 TTL。

## 6. 问答集导入

```
POST /evaluations/sets/import
{"eval_group": "支付域-v1", "cases": [
  {"query": "...", "expected_sources": ["文档标题"],
   "question_type": "A", "expected_keywords": []}
]}
```

幂等（group+query 去重）；expected_sources 不存在记 warning 不拦截。

## 7. 策略对比实验

```
POST /experiments {name, document_ids, policy_ids[], eval_group}
  → N 个 Run（每策略一个），Celery 分发
  → 每 Run：reprocess → run_evaluation(复用引擎) → metrics 落库
GET  /experiments/{id}  → policy × 指标 × by_type 分层矩阵
POST /experiments/{id}/apply → P0 记操作日志，P2 接审批门
```

经典产出（证明"没有全局最优切分，只有按文档类型的局部最优"）：

```
        heading  fixed  clause  qa_pair  parent_child
A 事实型
B 流程型
D 多跳
G 表格计算(新增)
```

## 8. API 清单

```
POST/GET  /ingest-policies            策略 CRUD
POST      /evaluations/sets/import    问答集导入
POST/GET  /experiments                创建 / 查对比矩阵
POST      /experiments/{id}/apply     应用胜出策略
```

## 9. 关键设计决策

1. **策略存快照不存引用**——实验可复现性 > 存储冗余
2. **claim/embedding 按内容 hash 缓存**——多轮实验的经济前提，
   重调参成本从全量 LLM 降到切分+少量新增切片
3. **实验副本同库打标隔离**——评测引擎零改动，比多库方案简单一个量级
4. **mock 迭代、真实验证两轮制**——沿用消融实验方法论
5. **状态机管可靠性，策略管决策**——管线只增 resolve_policy 一个查询点
6. **表格"向量路由 + SQL 取数"双形态**——向量找表、SQL 算数，
   各做自己擅长的事；AST 白名单约束 Text-to-SQL 注入面
7. **chunk_params JSON + CRUD 时 schema 校验**——可校验的 PipelineConfig，
   与 Config Copilot 项目概念打通

## 10. 风险与开放问题

1. 副本膨胀：P0 手动清理，P1 加 TTL
2. 缓存命中率：跨策略 chunk 边界不同，statement 级缓存命中可能偏低，
   P1 实测后可降粒度到段落级
3. Text-to-SQL 可靠性：schema 进 prompt 需裁剪（列名+类型+3 行样例）；
   P0 限单表；mock 模板化保证测试确定性
4. schema 漂移：同名表重导入列变更 → 按内容 hash 建表，旧表标记过期
5. default 策略只读，防止默认值漂移

## 11. 待 review 决策点

- [ ] 实验副本隔离确认选 A（同库打标）
- [ ] claim 缓存粒度：statement hash（默认）vs segment hash
- [ ] P0 切分策略集：heading/fixed/recursive/page/table_row 是否合适
- [ ] table_mode 放 P1 是否接受（还是提前到 P0 只做物化不做查询）
