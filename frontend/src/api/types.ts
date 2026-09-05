/**
 * Domain types mirroring the backend schemas.
 *
 * Hand-written for now. Once the backend is running, `npm run api:types`
 * generates these from the OpenAPI schema instead, and any drift becomes a
 * compile error rather than a runtime surprise.
 */

export type TicketStatus =
  | 'open'
  | 'assigned'
  | 'in_progress'
  | 'on_hold'
  | 'resolved'
  | 'rejected'
  | 'closed'

export type Priority = 'p1' | 'p2' | 'p3' | 'p4'
export type Severity = 's1_critical' | 's2_major' | 's3_minor' | 's4_cosmetic'
export type Impact = 'whole_org' | 'department' | 'few_users' | 'single_user'
export type Workaround = 'none' | 'painful' | 'easy'
export type Environment = 'production' | 'staging' | 'uat'
export type RejectionReason = 'not_a_bug' | 'duplicate' | 'wont_fix' | 'cannot_reproduce'
export type WaitingOn = 'cs' | 'client' | 'other_team'
export type ClientTier = 'standard' | 'premium' | 'enterprise'

export interface NamedRef {
  id: string
  name: string
}

export interface UserRef {
  id: string
  full_name: string
}

export interface TicketListItem {
  id: string
  ticket_number: number
  reference: string
  title: string
  status: TicketStatus
  priority: Priority
  severity: Severity
  impact: Impact
  environment: Environment
  client: NamedRef | null
  team: NamedRef | null
  assignee: UserRef | null
  created_at: string
  updated_at: string
  reopen_count: number
  version: number
}

export interface TicketDetail extends TicketListItem {
  description: string
  steps_to_reproduce: string | null
  expected_result: string | null
  actual_result: string | null
  workaround: Workaround
  reporter_name: string | null
  reporter_email: string | null
  created_by: UserRef | null
  resolution_notes: string | null
  rejection_reason: RejectionReason | null
  on_hold_waiting_on: WaitingOn | null
  ado_work_item_url: string | null
  priority_overridden: boolean
  priority_override_reason: string | null
  resolved_at: string | null
  closed_at: string | null
  /** What the current user may do right now. UI convenience; server enforces. */
  available_actions: string[]
}

export interface StatusHistoryEntry {
  id: string
  from_status: TicketStatus | null
  to_status: TicketStatus
  changed_by: UserRef | null
  note: string | null
  changed_at: string
}

export interface Comment {
  id: string
  ticket_id: string
  body: string
  author: UserRef | null
  created_at: string
  edited_at: string | null
  can_edit: boolean
}

export interface Attachment {
  id: string
  original_filename: string
  content_type: string
  size_bytes: number
  uploaded_by_id: string
  created_at: string
  ticket_id: string | null
  comment_id: string | null
}

export interface Client {
  id: string
  name: string
  code: string
  tier: ClientTier
  notes: string | null
  is_active: boolean
  created_at: string
}

export interface Team {
  id: string
  name: string
  code: string
  description: string | null
  manager_id: string | null
  is_active: boolean
  member_count: number
  created_at: string
}

export interface AdminUser {
  id: string
  email: string
  full_name: string
  role: string
  is_active: boolean
  team_ids: string[]
  last_login_at: string | null
  created_at: string
}

export interface AuditEntry {
  id: string
  actor_id: string | null
  actor_type: string
  action: string
  entity_type: string
  entity_id: string | null
  changes: Record<string, unknown>
  created_at: string
}

export interface EnumOption {
  value: string
  label: string
  order: number
}

export interface MetaEnums {
  severities: EnumOption[]
  impacts: EnumOption[]
  workarounds: EnumOption[]
  priorities: EnumOption[]
  statuses: EnumOption[]
  environments: EnumOption[]
  rejection_reasons: EnumOption[]
  waiting_on: EnumOption[]
  client_tiers: EnumOption[]
  roles: EnumOption[]
}

export interface DashboardCounts {
  my_open: number
  team_inbox: number
  awaiting_closure: number
  unassigned: number
}

export interface TicketFilters {
  status?: TicketStatus[]
  priority?: Priority[]
  team_id?: string[]
  client_id?: string[]
  assignee_id?: string
  unassigned?: boolean
  awaiting_closure?: boolean
  environment?: Environment
  created_from?: string
  created_to?: string
  q?: string
  sort?: string
  page?: number
  page_size?: number
}

// ---------------------------------------------------------------- AI layer

/** Which search found a result. Shown to the reader, not just logged. */
export type RetrievalSource = 'semantic' | 'lexical' | 'reference'

/** The reranker's verdict on a candidate. Null when no model ran. */
export type Relation = 'duplicate' | 'related' | 'recurring' | 'unrelated'

export interface SimilarTicket {
  ticket_id: string
  reference: string
  title: string
  status: string
  priority: string
  team_id: string
  client_id: string
  created_at: string
  closed_at: string | null
  /** First part of the resolution notes, when the ticket was closed. */
  resolution_summary: string | null
  /** Fused rank score. Comparable within one response only. */
  score: number
  sources: RetrievalSource[]
  /** True when more than one search returned it — the strongest cheap signal. */
  agreed: boolean

  /** Set when this result was judged and stored, so it can be accepted or rejected. */
  suggestion_id: string | null
  /** Null when no model ran: the result is a search hit, not a claim. */
  relation: Relation | null
  confidence: number | null
  reason: string | null
}

export interface SimilarTicketsResponse {
  ticket_id: string
  results: SimilarTicket[]
  total_candidates: number
  /** Ticket numbers named explicitly in the text, e.g. from "same as OS-142". */
  references: number[]
  /** Which retrieval nodes ran and what each returned. Diagnostics. */
  trace: string[]
  /** True when one retriever was unavailable and results came from the other. */
  degraded: boolean
  /** Non-empty only when an input guardrail refused the request. */
  blocked_reason: string
  /** How retrieval was graded: good, weak, or empty. */
  grade: string
  /** How many corrective rewrite cycles ran. */
  rewrites: number
  /** Which model judged these. Null means retrieval only, no LLM involved. */
  rerank_model: string | null
  from_cache: boolean
}

export interface SuggestionDecisionResult {
  id: string
  ticket_id: string
  related_ticket_id: string | null
  relation: Relation | null
  confidence: number | null
  reason: string | null
  model: string | null
  status: 'pending' | 'accepted' | 'rejected' | 'expired'
  decided_at: string | null
}

export interface AnalysisStep {
  turn: number
  tool_name: string | null
  tool_args: Record<string, unknown> | null
  result_summary: string | null
  created_at: string
}

export interface AnalysisReport {
  summary: string
  likely_area: string
  is_recurring: boolean
  related_references: string[]
  suggested_next_steps: string[]
  confidence: number
}

export type AnalysisStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'budget_exceeded'
  | 'timed_out'

export interface AnalysisRun {
  id: string
  ticket_id: string
  status: AnalysisStatus
  /** Null while queued or running. */
  report: AnalysisReport | null
  /** True when a limit stopped the run; the report is what it had by then. */
  partial: boolean
  stop_reason: string | null
  model: string | null
  /** True when a weaker fallback model produced this. */
  used_fallback_model: boolean
  turns: number
  tool_calls: number
  estimated_cost: number
  latency_ms: number | null
  helpful: boolean | null
  steps: AnalysisStep[]
  trace: string[]
}

export interface Conversation {
  id: string
  title: string | null
  message_count: number
  created_at: string
  last_message_at: string | null
  expires_at: string
}

export interface ChatTurn {
  answer: string
  model: string | null
  tool_calls: number
  trace: string[]
  blocked_reason: string
  /** False when the checkpointer is unavailable and the thread forgets on restart. */
  persistent: boolean
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}
