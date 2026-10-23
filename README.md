# Merchant AI Knowledge OS

面向招商、商家入驻、品牌资质、退入驻、CRM、营销发券和客服排障的 AI 知识治理平台与 Agent Runtime。项目实现了“来源可追溯、知识可审核、检索可解释、执行可恢复”的核心链路。

## 已实现

- `SourceDocument → SourceSegment → SourceClaim → WikiPage / WikiSection` 分层知识模型
- Markdown/手工内容、文件上传、批量导入、飞书文档导入和历史 `KnowledgeItem + Chunk` 回填
- Wiki 编译：新建页面、增量合并、新增 Section、重复内容仅补来源、高风险变更进入 Review Gate
- Review 工作台：来源 Claim、原文片段、目标 Wiki 聚合，支持修改 Scope、Owner、拟合并内容和目标 Section
- Wiki-first 并行混合检索：Wiki 词法/向量、Source 词法/向量、Elasticsearch BM25、每分支超时与降级诊断
- 持久化实体关系 GraphRAG：跨 WikiPage/Document 路径扩展和 `graph_query`
- Agent Runtime：Celery 调度、计划校验、工具/action 白名单、风险审批、step 幂等、事件、结构化失败诊断、恢复、replay 和产物回流
- Evaluation Engine 与 Scorer Registry：`source_hit`、`wiki_hit`、`citation_validity`、`rag_no_hit`、latency/p95、`pass_rate`
- Vue 管理台、Alembic 增量迁移、Docker Compose 与 smoke 脚本

## 技术栈

FastAPI、Pydantic、SQLAlchemy/Alembic、PostgreSQL/pgvector、Elasticsearch、Redis/Celery、Vue 3、OpenAI-compatible Gateway（阿里云百炼）。本地快速模式使用 SQLite 和确定性 mock；生产容器使用 PostgreSQL、Elasticsearch、Redis 和 Celery 异步执行。

## 快速启动（本地轻量模式）

需要 Python 3.9+ 与 Node.js 20+。

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
AGENT_EXECUTION_MODE=sync
```

本地开发默认同步执行 Agent；完整容器模式使用 `AGENT_EXECUTION_MODE=celery` 投递到 Redis/Celery。飞书导入还需要：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=your-secret
FEISHU_BASE_URL=https://open.feishu.cn/open-apis
```

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

上传 Markdown：

```bash
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -F 'file=@rules.md' -F 'owner=算法质量' -F 'compile=true'
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

当前已具备核心链路和本地/容器降级路径。进一步上线建议补充 HNSW/IVFFlat 向量索引、独立图数据库或更细粒度语义关系抽取、OIDC/RBAC、对象存储、OpenTelemetry、真实 RAGAS/LLM-as-judge 服务和细粒度租户隔离。生产性能与 pre1 回归结果必须以对应环境的实测报告为准。
