/**
 * Navigation definition.
 *
 * One declarative array, filtered against the permission list from the session.
 * The effect is role-based, but the mechanism is permission-based: granting a
 * role a new permission changes the menu with no change here.
 *
 * See docs/07-frontend.md.
 */

import { Permission, type PermissionValue } from './permissions'

export interface NavItem {
  label: string
  path: string
  icon: string
  /** null means always visible. */
  permission: PermissionValue | null
  /** Renders a separator above this item. */
  startsGroup?: boolean
  /** Optional live count, wired up in Phase 6. */
  badgeKey?: 'teamInbox' | 'awaitingClosure'
}

export const NAV_ITEMS: NavItem[] = [
  { label: 'Dashboard', path: '/', icon: 'grid', permission: null },
  { label: 'My Tickets', path: '/tickets/mine', icon: 'person', permission: Permission.TICKET_READ },
  {
    label: 'Team Inbox',
    path: '/tickets/inbox',
    icon: 'inbox',
    permission: Permission.TICKET_ASSIGN,
    badgeKey: 'teamInbox',
  },
  {
    label: 'Awaiting Closure',
    path: '/tickets/awaiting',
    icon: 'check',
    permission: Permission.TICKET_CLOSE,
    badgeKey: 'awaitingClosure',
  },
  { label: 'All Tickets', path: '/tickets', icon: 'list', permission: Permission.TICKET_READ },
  // Reading tickets is the only permission it needs: the assistant has no
  // write tools, so it can never do anything the viewer could not do by hand.
  { label: 'Assistant', path: '/assistant', icon: 'chat', permission: Permission.TICKET_READ },

  {
    label: 'Clients',
    path: '/clients',
    icon: 'building',
    permission: Permission.CLIENT_MANAGE,
    startsGroup: true,
  },
  { label: 'Teams', path: '/teams', icon: 'people', permission: Permission.TEAM_MANAGE },
  { label: 'Users', path: '/users', icon: 'person-gear', permission: Permission.USER_MANAGE },
  { label: 'Audit Log', path: '/audit', icon: 'journal', permission: Permission.AUDIT_READ },

  {
    label: 'Tenants',
    path: '/platform/tenants',
    icon: 'diagram',
    permission: Permission.TENANT_MANAGE,
    startsGroup: true,
  },
]

export function visibleNavItems(permissions: readonly string[]): NavItem[] {
  return NAV_ITEMS.filter((item) => item.permission === null || permissions.includes(item.permission))
}
