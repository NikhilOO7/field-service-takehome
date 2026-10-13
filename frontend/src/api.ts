// Typed client for the FastAPI backend. Identity comes from the HttpOnly session cookie only.

export type User = {
  id: string; name: string; title: string | null; groups: string[]
  certifications?: string[]; is_lead?: boolean; can_approve?: boolean
}
export type Cited = { text: string; citations: string[] }
export type Block = { type: 'fact' | 'step' | 'warning' | 'history' | 'note'; text: string; citations: string[] }
export type Conflict = {
  conflict_id: string; topic: string
  positions: { text: string; citations: string[] }[]; resolution: Cited
}
export type Answer = {
  status: 'answered' | 'partial' | 'not_covered' | 'restricted' | 'clarify'
  headline: Cited
  blocks: Block[]
  conflicts: Conflict[]
  restricted_notice: { what_you_can_do: Cited[] } | null
  clarify_question: Cited | null
  follow_up: { needed: boolean; reasons: string[]; work_order_hint: unknown }
  confidence: 'high' | 'medium' | 'low'
}
export type Source = {
  id: string; chunk_id: string; label: string; doc_id: string; doc_type: string; doc_status: string
  locator: Record<string, unknown>; cited: boolean; text: string
}
export type Fact = { id: string; label: string; text: string; cited: boolean; chunk_ids?: string[] }
export type AskResponse = {
  message_id: string; conversation_id: string
  answer: Answer
  validation: Record<string, { status: string; reasons: string[] }>
  restricted: { required_certifications: string[]; procedures: { title: string; doc_number: string | null }[] } | null
  clarify_options: string[]
  assets: { asset_id: string; model: string; serial: string; site: string; region: string; firmware: string | null }[]
  bulletins: { asset_id: string; number: string; effect: string; campaign_code: string | null }[]
  history: Record<string, { summary: string[]; events: { date: string; source: string; ref: string; fault_code: string | null; notes: string }[] }>
  sources: Source[]
  facts: Fact[]
  user: { id: string; name: string; groups: string[] }
  timings: Record<string, number>
}
export type SourceDetail = {
  chunk_id: string; text: string; label: string; title: string; doc_id: string; doc_type: string
  doc_number: string | null; revision: string | null; effective_date: string | null; doc_status: string
  flags: string[]; file_type: string; locator: Record<string, unknown>
  context: { id: string; text: string }[]
}
export type WOField = { value: unknown; provenance: 'system' | 'answer' | 'technician'; citations: string[] }
export type WorkOrder = {
  id: string; status: 'draft' | 'pending_approval' | 'approved' | 'rejected' | 'sent'
  created_by: string; approved_by: string | null; rejected_reason: string | null
  payload: Record<string, WOField>
  validation: { field: string; severity: 'error' | 'warning'; message: string }[]
  events?: { actor: string; action: string; diff: unknown; at: string }[]
  source_message?: string
}

export interface Asset { asset_id: string; model: string; serial: string; site_name: string | null }

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : (detail as { message?: string })?.message ?? `HTTP ${status}`)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail: unknown = res.statusText
    try { detail = (await res.json()).detail } catch { /* not json */ }
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  users: () => request<{ users: User[]; groups: Record<string, string> }>('/api/users'),
  me: () => request<User>('/api/session'),
  login: (user_id: string) => request<User>('/api/session', { method: 'POST', body: JSON.stringify({ user_id }) }),
  conversations: () =>
    request<{ conversations: { id: string; created_at: string; first_question: string | null }[] }>('/api/conversations'),
  conversation: (id: string) =>
    request<{ id: string; messages: { id: string; question: string; answer: AskResponse }[] }>(`/api/conversations/${id}`),
  source: (chunkId: string) => request<SourceDetail>(`/api/sources/${chunkId}`),
  assets: () => request<{ assets: Asset[] }>('/api/assets'),
  workOrders: () => request<{ work_orders: WorkOrder[]; can_approve: boolean }>('/api/work-orders'),
  workOrder: (id: string) => request<WorkOrder>(`/api/work-orders/${id}`),
  createWorkOrder: (message_id: string) =>
    request<WorkOrder>('/api/work-orders', { method: 'POST', body: JSON.stringify({ message_id }) }),
  editWorkOrder: (id: string, changes: Record<string, unknown>) =>
    request<WorkOrder>(`/api/work-orders/${id}`, { method: 'PATCH', body: JSON.stringify({ changes }) }),
  submitWorkOrder: (id: string) => request<WorkOrder>(`/api/work-orders/${id}/submit`, { method: 'POST' }),
  approveWorkOrder: (id: string) => request<WorkOrder>(`/api/work-orders/${id}/approve`, { method: 'POST' }),
  rejectWorkOrder: (id: string, reason: string) =>
    request<WorkOrder>(`/api/work-orders/${id}/reject`, { method: 'POST', body: JSON.stringify({ reason }) }),
  outbox: () => request<{ outbox: { id: number; work_order_id: string; payload: unknown; created_at: string }[] }>('/api/outbox'),
  fileUrl: (docId: string, page?: unknown) => `/api/documents/${docId}/file${page ? `#page=${page}` : ''}`,
}

// POST /api/ask?stream=1 returns server-sent events; EventSource can't POST, so read the stream manually.
export async function askStream(
  question: string,
  conversationId: string | null,
  onStatus: (message: string) => void,
): Promise<AskResponse> {
  const res = await fetch('/api/ask?stream=true', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ question, conversation_id: conversationId }),
  })
  if (!res.ok || !res.body) {
    let detail: unknown = res.statusText
    try { detail = (await res.json()).detail } catch { /* ignore */ }
    throw new ApiError(res.status, detail)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const raw = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      const event = /^event: (.*)$/m.exec(raw)?.[1]
      const data = /^data: (.*)$/m.exec(raw)?.[1]
      if (!event || !data) continue
      const parsed = JSON.parse(data)
      if (event === 'status') onStatus(parsed.message)
      if (event === 'answer') return parsed as AskResponse
      if (event === 'error') throw new ApiError(parsed.status, parsed.message)
    }
  }
  throw new ApiError(500, 'stream ended without an answer')
}
