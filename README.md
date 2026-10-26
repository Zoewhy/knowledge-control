# Merchant AI Knowledge OS

面向招商、商家入驻、品牌资质、退入驻、CRM、营销发券和客服排障的 AI 知识治理平台与 Agent Runtime。项目实现了“来源可追溯、知识可审核、检索可解释、执行可恢复”的核心链路。

## 已实现

- `SourceDocument → SourceSegment → SourceClaim → WikiPage / WikiSection` 分层知识模型
- 多模态入库链路：Markdown/TXT、PDF（PyMuPDF 文本/表格提取，扫描页渲染成图片走 VL 描述）、DOCX（Heading 层级重建）、XLSX（表头+行转 Markdown 表格）、PNG/JPG（VL 图片描述）统一解析为保序块序列；filetype 嗅探真实 MIME（伪造扩展名 422），原文按 sha256 分目录落盘（`DocumentFile`）
- 入库状态机：`pending → parsing → chunking → extracting → indexing → compiling → processed`，每步幂等可断点续跑，失败置 `failed + error_message`，支持 `GET /documents/{id}/status` 查询进度与 `POST /documents/{id}/retry` 重试；`INGEST_EXECUTION_MODE=celery` 时投递 Celery 异步执行
- 切分升级：tiktoken（cl100k_base，内置离线 BPE 资产）真实 token 计数，512 token / 50 overlap 二次切分，segment 保留 `heading_path` 与 `segment_type`（text/table/image），表格整块成段不拆
- 手工内容、批量导入、飞书文档导入和历史 `KnowledgeItem + Chunk` 回填保持同步入库不变
- Wiki 编译：新建页面、增量合并、新增 Section、重复内容仅补来源、高风险变更进入 Review Gate
- Review 工作台：来源 Claim、原文片段、目标 Wiki 聚合，支持修改 Scope、Owner、拟合并内容和目标 Section
- Wiki-first 并行混合检索：Wiki 词法/向量、Source 词法/向量、Elasticsearch BM25、每分支超时与降级诊断
- 持久化实体关系 GraphRAG：生产环境写入 Neo4j（`Entity` / `Section` / `CO_OCCURS` 关系），本地未配置时降级为 SQL 共现图；支持跨 WikiPage/Document 路径扩展和 `graph_query`
- Agent Runtime：LangGraph `StateGraph` 编排确定性计划（审批门/幂等/恢复），Celery 调度、计划校验、工具/action 白名单、风险审批、step 幂等、事件、结构化失败诊断、恢复、replay 和产物回流
- Integration Gateway：飞书事件订阅（URL 验证、token 校验、事件幂等去重），消息指令路由（`文档 <token>` / `问答:` / `用例:` / `归因:`）到知识接入、问答、测试生成和告警归因任务
- QA 测试闭环：`prd_parse → knowledge_search → testpoint_generate → testcase_generate → coverage_review → report_generate`，结构化用例入库（`QaRequirement / QaTestSuite / QaTestCase`），Markdown 报告产物可回流知识库
- 告警归因：通用告警 Webhook（fingerprint 幂等、重复告警合并计数）、`alert_context_collect → knowledge_search → graph_query → incident_hypothesis_generate → incident_record_create` 归因链，高风险事故记录创建需人工审批，输出仅包含根因假设与不确定性说明
- Evaluation Engine 与 Scorer Registry：`source_hit`、`wiki_hit`、`citation_validity`、`rag_no_hit`、latency/p95、`pass_rate`
- Vue 管理台（含集成事件、QA Studio、告警归因视图）、Alembic 增量迁移、Docker Compose 与 smoke 脚本

## 技术栈

FastAPI、Pydantic、SQLAlchemy/Alembic、PolarDB PostgreSQL/pgvector、Elasticsearch、Neo4j（GraphRAG 图存储）、Redis/Celery、LangChain（Chat/Embedding、文档切分、RAG 链）、LangGraph（Agent Runtime）、PyMuPDF / python-docx / openpyxl（多格式解析）、tiktoken（token 计数）、Vue 3、OpenAI-compatible Gateway（阿里云百炼，含 qwen-vl-plus 视觉模型）。PolarDB PostgreSQL 与社区 PostgreSQL 协议兼容，本地快速模式使用 SQLite 和确定性 mock；生产容器使用 PostgreSQL（可替换为 PolarDB 连接串）、Elasticsearch、Neo4j、Redis 和 Celery 异步执行。

## 快速启动（本地轻量模式）

需要 Python 3.10+ 与 Node.js 20+。

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
alembic upgrade head
PYTHONPATH=backend python -m app.scripts.production_seed --scale standard
make api
```

另开终端：

```bash
cd frontend
npm install
npm run dev
```

访问 `http://localhost:5173`，API 文档位于 `http://localhost:8000/docs`。

## 配置

项目不会把 API Key 写进源码。将以下变量放入本机 `.env`：

```dotenv
BAILIAN_API_KEY=your-key
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
BAILIAN_CHAT_MODEL=qwen-plus
BAILIAN_EMBEDDING_MODEL=text-embedding-v3
BAILIAN_EMBEDDING_DIMENSIONS=1024
LLM_MOCK_MODE=false
ENABLE_ELASTICSEARCH=false
ENABLE_NEO4J=false
AGENT_EXECUTION_MODE=sync
INGEST_EXECUTION_MODE=sync
UPLOAD_DIR=data/files
UPLOAD_MAX_MB=20
BAILIAN_VL_MODEL=qwen-vl-plus
```

图存储默认使用 SQL 共现图（零外部依赖）。配置 `ENABLE_NEO4J=true` 及 `NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD` 后切换到 Neo4j；Neo4j 不可达时自动降级回 SQL 图。生产数据库将 `DATABASE_URL` 指向 PolarDB PostgreSQL 连接串即可，驱动与 SQL 方言完全兼容。

本地开发默认同步执行 Agent；完整容器模式使用 `AGENT_EXECUTION_MODE=celery` 投递到 Redis/Celery。飞书导入还需要：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=your-secret
FEISHU_BASE_URL=https://open.feishu.cn/open-apis
```

飞书事件订阅（Integration Gateway）额外配置：

```dotenv
FEISHU_VERIFICATION_TOKEN=事件订阅的 verification token
FEISHU_DEFAULT_CHAT_ID=任务完成通知的默认群 chat_id
FEISHU_NOTIFY_ON_TASK_DONE=true
```

注意：当前版本仅支持明文事件订阅；飞书 Encrypt Key 加密事件尚未实现解密（`FEISHU_ENCRYPT_KEY` 仅为预留配置）。告警平台通过通用 Webhook `POST /api/v1/integrations/alerts` 接入，未接入真实监控平台时归因结果仅为“根因假设”。

未配置模型 Key 或 `LLM_MOCK_MODE=true` 时，Claim 抽取、用例生成与回答使用确定性 mock。未配置飞书凭证时，飞书接口会返回明确的配置错误，不会静默使用假数据。

## 完整容器启动

```bash
cp .env.example .env
# 编辑 .env，按需填入百炼与飞书配置
docker compose up --build
docker compose exec api python -m app.scripts.production_seed --scale standard
```

管理台：`http://localhost:8080`；API：`http://localhost:8000`。

## 历史迁移与索引重建

若旧库存在 `knowledge_items` 和 `chunks`/`knowledge_chunks`，可幂等回填到新知识模型：

```bash
make backfill
```

只重建向量与 Elasticsearch 索引：

```bash
PYTHONPATH=backend python -m app.scripts.backfill_legacy --rebuild-only
```

脚本不删除旧表。Agent 产物也可通过管理台或 `POST /api/v1/agent/artifacts/{artifact_id}/reflow` 固化为知识文档并进入编译链路。

## API 示例

上传文件（Markdown / TXT / PDF / DOCX / XLSX / PNG / JPG，≤20MB）：

```bash
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -F 'file=@rules.pdf' -F 'owner=算法质量' -F 'compile=true'

# 查询入库进度 / 失败重试
curl http://localhost:8000/api/v1/documents/{document_id}/status
curl -X POST http://localhost:8000/api/v1/documents/{document_id}/retry
```

可追溯问答：

```bash
curl -X POST http://localhost:8000/api/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"经销商授权链路断裂时怎么处理？","top_k":8,"include_answer":true}'
```

## 演示数据

数据生成器覆盖商家入驻主体审核、品牌方与经销商授权、AI 法务机审、退入驻卡单、DCheck 对账、招商 CRM、MySQL/ES 同步、营销发券、邀请码治理和智能助手 Badcase。

```bash
PYTHONPATH=backend python -m app.scripts.production_seed --scale small     # 44 份
PYTHONPATH=backend python -m app.scripts.production_seed --scale standard  # 147 份
PYTHONPATH=backend python -m app.scripts.production_seed --scale full      # 277 份
```

生成器具有幂等性。三份公开法规摘要保留权威来源链接；其余 SOP、审核案例、事故复盘和 Agent Badcase 均为“合成数据”“已脱敏”，不代表任何公司的真实规则或线上记录。

## 验证

```bash
pytest -q
ruff check backend
cd frontend && npm run build
python backend/scripts/smoke.py
```

## 后续生产化建议

当前已具备核心链路和本地/容器降级路径。进一步上线建议补充 HNSW/IVFFlat 向量索引、LLM 实体链接与语义谓词抽取（替换共现边）、OIDC/RBAC、对象存储、OpenTelemetry、真实 RAGAS/LLM-as-judge 服务和细粒度租户隔离。生产性能与 pre1 回归结果必须以对应环境的实测报告为准。
