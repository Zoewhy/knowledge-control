export const baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api/v1'

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(init?.headers || {}),
    },
  })
  if (!response.ok) {
    const body = await response.text()
    throw new Error(body || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

export type Stats = {
  documents: number
  segments: number
  claims: number
  wiki_pages: number
  pending_reviews: number
  agent_tasks: number
  evaluation_runs: number
}

export type KnowledgeDocument = {
  id: string
  title: string
  source_type: string
  owner: string
  status: string
  progress: number
  error_message?: string | null
  created_at: string
  segments?: Array<{ id: string; heading?: string | null; segment_type: string }>
}

export type Review = {
  id: string
  claim_id: string
  target_page_id?: string | null
  target_section_id?: string | null
  action: string
  reason: string
  proposed_content: string
  current_content: string
  risk_score: number
  status: string
  created_at: string
  source_context: {
    statement: string
    scope: string
    confidence: number
    segment_content: string
    document_title: string
    source_uri?: string | null
  }
  target_wiki: {
    page_title?: string | null
    section_heading?: string | null
  }
}

export type WikiPage = {
  id: string
  title: string
  summary: string
  owner: string
  version: number
  sections: Array<{
    id: string
    heading: string
    content: string
    citations: Array<{ document_title: string; statement: string; source_uri?: string }>
  }>
}

export type SearchResponse = {
  answer?: string
  latency_ms: number
  diagnostics: Record<string, unknown>
  hits: Array<{
    channel: string
    score: number
    title: string
    content: string
    source?: { document_title: string; statement: string }
  }>
}

export type AgentArtifact = {
  id: string
  name: string
  artifact_type: string
  content: string
  document_id?: string | null
}

export type AgentTask = {
  id: string
  instruction: string
  status: string
  plan: Array<{ tool: string }>
  result: Record<string, unknown>
  error?: string | null
  error_detail: Record<string, unknown>
  tool_calls: Array<{ tool_name: string; risk_level: string; status: string }>
  events: Array<{
    id: string
    step_index?: number | null
    event_type: string
    payload: Record<string, unknown>
    created_at: string
  }>
  artifacts: AgentArtifact[]
  created_at: string
}

export type EvaluationRun = {
  id: string
  status: string
  metrics: Record<string, unknown>
  created_at: string
  finished_at?: string | null
  results: Array<{
    id: string
    case_id: string
    metrics: Record<string, unknown>
    answer: string
    trace: Record<string, unknown>
  }>
}

export type IntegrationEvent = {
  id: string
  provider: string
  event_id?: string | null
  event_type: string
  status: string
  agent_task_id?: string | null
  error?: string | null
  created_at: string
}

export type QaRequirement = {
  id: string
  title: string
  content: string
  parsed: Record<string, unknown>
  created_at: string
}

export type QaTestSuite = {
  id: string
  requirement_id: string
  task_id?: string | null
  name: string
  status: string
  summary: Record<string, unknown>
  testpoints: Array<{ name: string; type: string; priority: string; rationale?: string }>
  cases: Array<{
    id: string
    name: string
    priority: string
    precondition: string
    steps: string[]
    expected_result: string
  }>
  created_at: string
}

export type AlertEvent = {
  id: string
  title: string
  service: string
  severity: string
  status: string
  description: string
  occurrence_count: number
  triage: Record<string, any>
  agent_task_id?: string | null
  created_at: string
}

export type IncidentRecord = {
  id: string
  alert_id: string
  title: string
  severity: string
  status: string
  summary: string
  root_cause_hypothesis: string
  impact: string
  recommended_actions: string[]
  created_at: string
}

