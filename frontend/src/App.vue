<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  AgentArtifact,
  AgentTask,
  api,
  EvaluationRun,
  Review,
  SearchResponse,
  Stats,
  WikiPage,
} from './api'

type View = 'overview' | 'knowledge' | 'review' | 'search' | 'agents' | 'evaluation'
type ReviewDraft = {
  scope: string
  owner: string
  proposedContent: string
  targetSectionId: string
}

const view = ref<View>('overview')
const loading = ref(false)
const error = ref('')
const notice = ref('')
const stats = ref<Stats | null>(null)
const wiki = ref<WikiPage[]>([])
const reviews = ref<Review[]>([])
const reviewDrafts = ref<Record<string, ReviewDraft>>({})
const tasks = ref<AgentTask[]>([])
const evaluationRuns = ref<EvaluationRun[]>([])
const selectedWiki = ref<WikiPage | null>(null)
const query = ref('品牌经销商授权链路不完整时，应该如何审核和补充材料？')
const searchResult = ref<SearchResponse | null>(null)
const instruction = ref('根据知识库为品牌授权范围超出上游权限设计测试用例')
const runMetrics = ref<Record<string, unknown> | null>(null)
const uploadFile = ref<File | null>(null)
const uploadTitle = ref('')
const uploadOwner = ref('unknown')
const uploadCompile = ref(true)
const batchJson = ref('[\n  {\n    "title": "文档标题",\n    "content": "Markdown 或文本内容",\n    "owner": "unknown",\n    "compile": true\n  }\n]')
const feishuToken = ref('')
const feishuTitle = ref('')
const feishuOwner = ref('unknown')
const feishuCompile = ref(true)
const expandedTasks = ref<Record<string, boolean>>({})

const nav = [
  ['overview', '总览', '01'],
  ['knowledge', '知识图谱', '02'],
  ['review', '审核队列', '03'],
  ['search', '可追溯问答', '04'],
  ['agents', 'Agent Runtime', '05'],
  ['evaluation', '评测实验', '06'],
] as const

const viewTitle = computed(() => nav.find((item) => item[0] === view.value)?.[1] || '')
const pendingReviews = computed(() => reviews.value.filter((item) => item.status === 'pending'))

function message(e: unknown, fallback: string) {
  return e instanceof Error ? e.message : fallback
}

function stringify(value: unknown) {
  return JSON.stringify(value, null, 2)
}

function formatMetric(value: unknown) {
  return typeof value === 'number' ? Number(value.toFixed(4)).toString() : String(value ?? '—')
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [statsData, wikiData, reviewData, taskData, evaluationData] = await Promise.all([
      api<Stats>('/dashboard'),
      api<WikiPage[]>('/wiki'),
      api<Review[]>('/reviews'),
      api<AgentTask[]>('/agent/tasks'),
      api<EvaluationRun[]>('/evaluations/runs'),
    ])
    stats.value = statsData
    wiki.value = wikiData
    reviews.value = reviewData
    tasks.value = taskData
    evaluationRuns.value = evaluationData
    reviewDrafts.value = Object.fromEntries(
      reviewData.map((item) => [
        item.id,
        {
          scope: item.source_context.scope,
          owner: wikiData.find((page) => page.id === item.target_page_id)?.owner || 'unknown',
          proposedContent: item.proposed_content,
          targetSectionId: item.target_section_id || '',
        },
      ]),
    )
    selectedWiki.value = wikiData.find((page) => page.id === selectedWiki.value?.id) || wikiData[0] || null
    if (!runMetrics.value && evaluationData.length) runMetrics.value = evaluationData[0].metrics
  } catch (e) {
    error.value = message(e, '加载失败')
  } finally {
    loading.value = false
  }
}

async function perform(action: () => Promise<void>, fallback: string) {
  loading.value = true
  error.value = ''
  notice.value = ''
  try {
    await action()
  } catch (e) {
    error.value = message(e, fallback)
  } finally {
    loading.value = false
  }
}

function selectFile(event: Event) {
  uploadFile.value = (event.target as HTMLInputElement).files?.[0] || null
}

async function uploadDocument() {
  if (!uploadFile.value) {
    error.value = '请选择 Markdown 或 TXT 文件。'
    return
  }
  await perform(async () => {
    const body = new FormData()
    body.append('file', uploadFile.value as File)
    body.append('title', uploadTitle.value.trim())
    body.append('owner', uploadOwner.value.trim() || 'unknown')
    body.append('compile', String(uploadCompile.value))
    await api('/documents/upload', { method: 'POST', body })
    notice.value = '文档上传完成。'
    uploadFile.value = null
    uploadTitle.value = ''
    await load()
  }, '上传失败')
}

async function importBatch() {
  let documents: unknown
  try {
    const parsed: unknown = JSON.parse(batchJson.value)
    documents = Array.isArray(parsed)
      ? parsed
      : typeof parsed === 'object' && parsed !== null && 'documents' in parsed
        ? (parsed as { documents: unknown }).documents
        : null
    if (!Array.isArray(documents) || !documents.length) throw new Error()
  } catch {
    error.value = '请输入非空文档数组，或包含 documents 数组的 JSON 对象。'
    return
  }
  await perform(async () => {
    const result = await api<{ total: number }>('/documents/batch', {
      method: 'POST',
      body: JSON.stringify({ documents }),
    })
    notice.value = `已处理 ${result.total} 篇文档。`
    await load()
  }, '批量导入失败')
}

async function importFeishu() {
  if (!feishuToken.value.trim()) {
    error.value = '请输入飞书 document_token。'
    return
  }
  await perform(async () => {
    await api('/documents/feishu', {
      method: 'POST',
      body: JSON.stringify({
        document_token: feishuToken.value.trim(),
        title: feishuTitle.value.trim() || null,
        owner: feishuOwner.value.trim() || 'unknown',
        compile: feishuCompile.value,
      }),
    })
    notice.value = '飞书文档导入完成。'
    feishuToken.value = ''
    feishuTitle.value = ''
    await load()
  }, '飞书导入失败')
}

async function doSearch() {
  await perform(async () => {
    searchResult.value = await api<SearchResponse>('/search', {
      method: 'POST',
      body: JSON.stringify({ query: query.value, top_k: 8, include_answer: true }),
    })
  }, '检索失败')
}

async function decide(item: Review, decision: 'approve' | 'reject' | 'source_only') {
  const draft = reviewDrafts.value[item.id]
  if (!draft) return
  if (!draft.scope.trim() || !draft.owner.trim() || !draft.proposedContent.trim()) {
    error.value = 'Scope、Owner 和 proposed_content 均不能为空。'
    return
  }
  if (decision === 'approve' && !draft.targetSectionId) {
    error.value = '批准合并前请选择目标 Section。'
    return
  }
  await perform(async () => {
    await api(`/reviews/${item.id}/decision`, {
      method: 'POST',
      body: JSON.stringify({
        decision,
        reviewer: 'admin',
        note: '在 Review 工作台处理',
        scope: draft.scope.trim(),
        owner: draft.owner.trim(),
        target_section_id: draft.targetSectionId || null,
        proposed_content: draft.proposedContent.trim(),
      }),
    })
    notice.value = '审核决定已提交。'
    await load()
  }, '审核失败')
}

async function createAgent() {
  if (!instruction.value.trim()) return
  await perform(async () => {
    await api('/agent/tasks', {
      method: 'POST',
      body: JSON.stringify({ instruction: instruction.value.trim(), auto_run: true }),
    })
    notice.value = 'Agent 任务已创建。'
    await load()
  }, '任务创建失败')
}

async function taskAction(task: AgentTask, action: 'approve' | 'resume' | 'replay') {
  await perform(async () => {
    await api(`/agent/tasks/${task.id}/${action}`, { method: 'POST' })
    expandedTasks.value[task.id] = true
    notice.value = action === 'replay' ? 'Replay 任务已创建。' : '任务状态已更新。'
    await load()
  }, '任务操作失败')
}

async function reflowArtifact(artifact: AgentArtifact) {
  await perform(async () => {
    await api(`/agent/artifacts/${artifact.id}/reflow`, { method: 'POST' })
    notice.value = '产物已回流至知识库。'
    await load()
  }, '产物回流失败')
}

async function runEvaluation() {
  await perform(async () => {
    const run = await api<EvaluationRun>('/evaluations/runs', { method: 'POST' })
    runMetrics.value = run.metrics
    notice.value = 'Benchmark 运行完成。'
    await load()
  }, '评测失败')
}

onMounted(load)
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">MA</div>
        <div><strong>MERCHANT AI</strong><span>KNOWLEDGE OS</span></div>
      </div>
      <nav>
        <button v-for="item in nav" :key="item[0]" :class="{ active: view === item[0] }" @click="view = item[0]">
          <span>{{ item[2] }}</span>{{ item[1] }}
          <b v-if="item[0] === 'review' && stats?.pending_reviews">{{ stats.pending_reviews }}</b>
        </button>
      </nav>
      <div class="system-status">
        <i></i>
        <div><strong>系统在线</strong><span>HYBRID RETRIEVAL READY</span></div>
      </div>
    </aside>

    <main>
      <header>
        <div><p>MERCHANT PLATFORM / AI CONTROL PLANE</p><h1>{{ viewTitle }}</h1></div>
        <button class="ghost" :disabled="loading" @click="load">{{ loading ? '处理中…' : '刷新数据' }}</button>
      </header>
      <div v-if="error" class="error">{{ error }}</div>
      <div v-if="notice" class="notice">{{ notice }}</div>

      <section v-if="view === 'overview'" class="view">
        <div class="hero">
          <div><span class="eyebrow">KNOWLEDGE → DECISION → ACTION</span><h2>让每一次商家审核<br />都有据可查。</h2><p>面向招商、商家入驻、品牌资质、退入驻和客服排障的知识治理与 Agent Runtime。规则可追溯，变更可审核，任务可恢复。</p></div>
          <div class="orb"><span>{{ stats?.claims || 0 }}</span><small>ACTIVE CLAIMS</small></div>
        </div>
        <div class="metrics">
          <article><span>01 / SOURCES</span><strong>{{ stats?.documents || 0 }}</strong><p>接入文档</p></article>
          <article><span>02 / KNOWLEDGE</span><strong>{{ stats?.wiki_pages || 0 }}</strong><p>Wiki 页面</p></article>
          <article><span>03 / GOVERNANCE</span><strong>{{ stats?.pending_reviews || 0 }}</strong><p>待审核变更</p></article>
          <article><span>04 / RUNTIME</span><strong>{{ stats?.agent_tasks || 0 }}</strong><p>Agent 任务</p></article>
        </div>
        <div class="flow-card">
          <div class="section-head"><span>LIVE PIPELINE</span><h3>知识生产链路</h3></div>
          <div class="pipeline">
            <div><b>01</b><strong>Source</strong><span>SOP / 案例 / 复盘</span></div><i>→</i>
            <div><b>02</b><strong>Claim</strong><span>原子声明抽取</span></div><i>→</i>
            <div><b>03</b><strong>Review</strong><span>冲突与风险门禁</span></div><i>→</i>
            <div><b>04</b><strong>Wiki</strong><span>可信知识编译</span></div><i>→</i>
            <div><b>05</b><strong>Agent</strong><span>受控任务执行</span></div>
          </div>
        </div>
      </section>

      <section v-else-if="view === 'knowledge'" class="view">
        <div class="section-head"><span>INGESTION</span><h3>接入知识来源</h3><p>上传本地文件、批量提交 JSON，或导入飞书文档。</p></div>
        <div class="ingest-grid">
          <form class="ingest-card" @submit.prevent="uploadDocument">
            <span class="eyebrow">MARKDOWN / TXT</span><h3>文件上传</h3>
            <label>文件<input type="file" accept=".md,.markdown,.txt" @change="selectFile" /></label>
            <label>标题（可选）<input v-model="uploadTitle" placeholder="默认使用文件名" /></label>
            <label>Owner<input v-model="uploadOwner" /></label>
            <label class="check"><input v-model="uploadCompile" type="checkbox" />上传后立即编译</label>
            <button class="primary" :disabled="loading" type="submit">上传文档</button>
          </form>
          <form class="ingest-card batch-card" @submit.prevent="importBatch">
            <span class="eyebrow">BATCH JSON</span><h3>批量导入</h3>
            <label>文档数组<textarea v-model="batchJson" rows="9" spellcheck="false"></textarea></label>
            <button class="primary" :disabled="loading" type="submit">提交批量文档</button>
          </form>
          <form class="ingest-card" @submit.prevent="importFeishu">
            <span class="eyebrow">FEISHU</span><h3>飞书导入</h3>
            <label>Document token<input v-model="feishuToken" placeholder="doccn…" /></label>
            <label>标题（可选）<input v-model="feishuTitle" /></label>
            <label>Owner<input v-model="feishuOwner" /></label>
            <label class="check"><input v-model="feishuCompile" type="checkbox" />导入后立即编译</label>
            <button class="primary" :disabled="loading" type="submit">导入飞书文档</button>
          </form>
        </div>
        <div class="split knowledge-browser">
          <div class="list-panel">
            <div class="section-head"><span>COMPILED KNOWLEDGE</span><h3>Wiki 页面</h3></div>
            <button v-for="page in wiki" :key="page.id" class="list-item" :class="{ selected: selectedWiki?.id === page.id }" @click="selectedWiki = page">
              <div><strong>{{ page.title }}</strong><span>{{ page.sections.length }} sections · v{{ page.version }}</span></div><em>→</em>
            </button>
            <div v-if="!wiki.length" class="empty">暂无已编译知识。</div>
          </div>
          <article v-if="selectedWiki" class="detail-panel">
            <span class="eyebrow">OWNER / {{ selectedWiki.owner }}</span><h2>{{ selectedWiki.title }}</h2><p class="lead">{{ selectedWiki.summary }}</p>
            <div v-for="section in selectedWiki.sections" :key="section.id" class="wiki-section">
              <h3>{{ section.heading }}</h3><p>{{ section.content }}</p>
              <div class="citations"><span v-for="citation in section.citations" :key="citation.statement">↗ {{ citation.document_title }}</span></div>
            </div>
          </article>
        </div>
      </section>

      <section v-else-if="view === 'review'" class="view">
        <div class="section-head"><span>HUMAN-IN-THE-LOOP</span><h3>高风险知识变更</h3><p>核对完整来源，调整适用范围与目标 Section 后审核。</p></div>
        <article v-for="item in pendingReviews" :key="item.id" class="review-card">
          <div class="review-meta"><span>{{ item.action }}</span><b>RISK {{ item.risk_score.toFixed(2) }}</b></div><h3>{{ item.reason }}</h3>
          <div class="source-context">
            <div><small>SOURCE / {{ item.source_context.document_title }}</small><p>{{ item.source_context.segment_content }}</p><a v-if="item.source_context.source_uri" :href="item.source_context.source_uri" target="_blank" rel="noreferrer">打开原始来源 ↗</a></div>
            <div><small>CLAIM · CONFIDENCE {{ item.source_context.confidence.toFixed(2) }}</small><p>{{ item.source_context.statement }}</p><span>TARGET / {{ item.target_wiki.page_title || '未指定页面' }} / {{ item.target_wiki.section_heading || '未指定 Section' }}</span></div>
          </div>
          <div class="diff"><div><small>CURRENT</small><p>{{ item.current_content || '新建知识' }}</p></div><div><small>PROPOSED</small><p>{{ item.proposed_content }}</p></div></div>
          <div v-if="reviewDrafts[item.id]" class="review-editor">
            <label>Scope<input v-model="reviewDrafts[item.id].scope" maxlength="200" /></label>
            <label>Owner<input v-model="reviewDrafts[item.id].owner" maxlength="120" /></label>
            <label>目标 Section<select v-model="reviewDrafts[item.id].targetSectionId"><option value="">请选择目标 Section</option><optgroup v-for="page in wiki" :key="page.id" :label="page.title"><option v-for="section in page.sections" :key="section.id" :value="section.id">{{ section.heading }}</option></optgroup></select></label>
            <label class="wide">Proposed content<textarea v-model="reviewDrafts[item.id].proposedContent" rows="4"></textarea></label>
          </div>
          <footer><button class="ghost" :disabled="loading" @click="decide(item, 'reject')">保留现有</button><button class="ghost" :disabled="loading" @click="decide(item, 'source_only')">仅关联来源</button><button class="primary" :disabled="loading" @click="decide(item, 'approve')">批准合并</button></footer>
        </article>
        <div v-if="!pendingReviews.length" class="empty large">审核队列已清空。</div>
      </section>

      <section v-else-if="view === 'search'" class="view">
        <div class="search-box"><span>ASK MERCHANT KNOWLEDGE</span><textarea v-model="query" rows="3"></textarea><button class="primary" :disabled="loading" @click="doSearch">执行混合检索 ↗</button></div>
        <div v-if="searchResult" class="answer"><div class="answer-head"><span>GROUNDED ANSWER</span><b>{{ searchResult.latency_ms }} ms</b></div><p>{{ searchResult.answer }}</p>
          <div class="hits"><article v-for="(hit, index) in searchResult.hits" :key="index"><div><b>[{{ index + 1 }}]</b><span>{{ hit.channel }}</span><em>{{ hit.score.toFixed(3) }}</em></div><h3>{{ hit.title }}</h3><p>{{ hit.content }}</p><small v-if="hit.source">SOURCE / {{ hit.source.document_title }}</small></article></div>
        </div>
      </section>

      <section v-else-if="view === 'agents'" class="view">
        <div class="composer"><span>NEW AGENT TASK</span><input v-model="instruction" @keyup.enter="createAgent" /><button class="primary" :disabled="loading" @click="createAgent">创建并执行</button></div>
        <article v-for="task in tasks" :key="task.id" class="task-card">
          <div class="task-head"><span :class="['status', task.status]">{{ task.status }}</span><time>{{ new Date(task.created_at).toLocaleString() }}</time></div>
          <h3>{{ task.instruction }}</h3><div class="plan"><span v-for="step in task.plan" :key="step.tool">{{ step.tool }}</span></div>
          <div v-if="task.error" class="task-error"><p class="error-text">{{ task.error }}</p><pre v-if="Object.keys(task.error_detail || {}).length">{{ stringify(task.error_detail) }}</pre></div>
          <footer class="task-actions"><button class="ghost" @click="expandedTasks[task.id] = !expandedTasks[task.id]">{{ expandedTasks[task.id] ? '收起详情' : `详情（${task.events?.length || 0} 事件 / ${task.artifacts?.length || 0} 产物）` }}</button><button v-if="['failed', 'running', 'pending'].includes(task.status)" class="ghost" :disabled="loading" @click="taskAction(task, 'resume')">恢复任务</button><button class="ghost" :disabled="loading" @click="taskAction(task, 'replay')">Replay</button><button v-if="task.status === 'waiting_approval'" class="primary" :disabled="loading" @click="taskAction(task, 'approve')">批准中风险工具</button></footer>
          <div v-if="expandedTasks[task.id]" class="task-detail">
            <section><h4>执行事件</h4><div v-if="task.events?.length" class="event-list"><article v-for="event in task.events" :key="event.id"><div><strong>{{ event.event_type }}</strong><span>STEP {{ event.step_index ?? '—' }} · {{ new Date(event.created_at).toLocaleString() }}</span></div><pre v-if="Object.keys(event.payload).length">{{ stringify(event.payload) }}</pre></article></div><p v-else class="empty">暂无事件。</p></section>
            <section><h4>任务产物</h4><article v-for="artifact in task.artifacts" :key="artifact.id" class="artifact"><div><strong>{{ artifact.name }}</strong><span>{{ artifact.artifact_type }}</span></div><pre>{{ artifact.content }}</pre><button class="ghost" :disabled="loading || !!artifact.document_id" @click="reflowArtifact(artifact)">{{ artifact.document_id ? '已回流知识库' : '回流到知识库' }}</button></article><p v-if="!task.artifacts?.length" class="empty">暂无产物。</p></section>
          </div>
        </article>
        <div v-if="!tasks.length" class="empty large">暂无 Agent 任务。</div>
      </section>

      <section v-else class="view">
        <div class="evaluation-hero"><span class="eyebrow">DETERMINISTIC FIRST</span><h2>商家任务分层评测</h2><p>覆盖主体审核、品牌授权、退入驻、CRM、发券与 Agent 工具调用，跟踪 wiki_hit、rag_no_hit、p95_latency_ms 与 pass_rate。</p><button class="primary" :disabled="loading" @click="runEvaluation">运行 Benchmark</button></div>
        <div v-if="runMetrics" class="metrics eval"><article v-for="(value, key) in runMetrics" :key="key"><span>{{ key }}</span><strong>{{ formatMetric(value) }}</strong></article></div>
        <div class="section-head history-head"><span>RUN HISTORY</span><h3>历史运行 · {{ evaluationRuns.length }}</h3></div>
        <article v-for="run in evaluationRuns" :key="run.id" class="run-card">
          <div class="run-head"><div><span :class="['status', run.status]">{{ run.status }}</span><strong>{{ new Date(run.created_at).toLocaleString() }}</strong></div><small>{{ run.id }}</small></div>
          <div class="run-metrics"><span v-for="(value, key) in run.metrics" :key="key">{{ key }} <b>{{ formatMetric(value) }}</b></span></div>
          <details><summary>查看 {{ run.results.length }} 条用例结果</summary><article v-for="result in run.results" :key="result.id" class="result-card"><small>CASE / {{ result.case_id }}</small><div class="run-metrics"><span v-for="(value, key) in result.metrics" :key="key">{{ key }} <b>{{ formatMetric(value) }}</b></span></div><p>{{ result.answer || '无回答' }}</p></article></details>
        </article>
        <div v-if="!evaluationRuns.length" class="empty large">暂无历史评测，运行 Benchmark 后将在此展示。</div>
      </section>
    </main>
  </div>
</template>
