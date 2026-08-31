/**
 * Permissions, mirroring the backend's map in docs/02-roles-and-permissions.md.
 *
 * IMPORTANT: everything here is UX only. Hiding a button or a nav item stops
 * people being shown doors they cannot open -- it does not lock any door. The
 * API enforces every one of these independently, and row-level security scopes
 * the data underneath that. Never treat a check here as a security boundary.
 */

export const Permission = {
  TENANT_MANAGE: 'tenant:manage',
  USER_MANAGE: 'user:manage',
  TEAM_MANAGE: 'team:manage',
  CLIENT_MANAGE: 'client:manage',

  TICKET_CREATE: 'ticket:create',
  TICKET_READ: 'ticket:read',
  TICKET_UPDATE: 'ticket:update',
  TICKET_ASSIGN: 'ticket:assign',
  TICKET_TRANSITION: 'ticket:transition',
  TICKET_RESOLVE: 'ticket:resolve',
  TICKET_CLOSE: 'ticket:close',
  TICKET_REOPEN: 'ticket:reopen',
  TICKET_REJECT: 'ticket:reject',
  TICKET_TRANSFER_TEAM: 'ticket:transfer_team',
  TICKET_OVERRIDE_PRIORITY: 'ticket:override_priority',

  COMMENT_CREATE: 'comment:create',
  ATTACHMENT_UPLOAD: 'attachment:upload',
  AUDIT_READ: 'audit:read',
} as const

export type PermissionValue = (typeof Permission)[keyof typeof Permission]

export const Role = {
  PLATFORM_SUPER_ADMIN: 'platform_super_admin',
  TENANT_ADMIN: 'tenant_admin',
  CS_LEAD: 'cs_lead',
  CS_AGENT: 'cs_agent',
  TEAM_MANAGER: 'team_manager',
  DEVELOPER: 'developer',
} as const

export type RoleValue = (typeof Role)[keyof typeof Role]

export const ROLE_LABELS: Record<RoleValue, string> = {
  [Role.PLATFORM_SUPER_ADMIN]: 'Platform Admin',
  [Role.TENANT_ADMIN]: 'Tenant Admin',
  [Role.CS_LEAD]: 'CS Lead',
  [Role.CS_AGENT]: 'CS Agent',
  [Role.TEAM_MANAGER]: 'Team Manager',
  [Role.DEVELOPER]: 'Developer',
}

export function hasPermission(
  granted: readonly string[],
  required: PermissionValue | null | undefined,
): boolean {
  if (!required) return true
  return granted.includes(required)
}

export function hasAnyPermission(
  granted: readonly string[],
  required: readonly PermissionValue[],
): boolean {
  return required.some((p) => granted.includes(p))
}
