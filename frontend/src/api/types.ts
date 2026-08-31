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
