import { Badge } from 'react-bootstrap'
import { NavLink } from 'react-router-dom'
import { useDashboardCounts } from '@/api/hooks/useTickets'
import { visibleNavItems } from '@/lib/nav'
import { usePermissions } from '@/lib/session'

/** Minimal inline glyphs — avoids an icon-font dependency for the shell. */
const ICONS: Record<string, string> = {
  grid: '▦',
  person: '👤',
  inbox: '📥',
  check: '✓',
  list: '☰',
  building: '🏢',
  people: '👥',
  'person-gear': '⚙',
  vector: '🧬',
  journal: '📋',
  diagram: '🗂',
}

interface SideNavProps {
  collapsed: boolean
}

export function SideNav({ collapsed }: SideNavProps) {
  const permissions = usePermissions()
  const items = visibleNavItems(permissions)
  const { data: counts } = useDashboardCounts()

  // With notifications deferred, these badges are the only signal that work is
  // waiting. Deliberate mitigation for accepted risk R-2.
  const badgeFor = (key?: string): number => {
    if (key === 'teamInbox') return counts?.team_inbox ?? 0
    if (key === 'awaitingClosure') return counts?.awaiting_closure ?? 0
    return 0
  }

  return (
    <aside className="os-sidebar">
      <a className="os-brand" href="/">
        <span className="os-brand-mark">OS</span>
        <span className="os-brand-text">Tracker</span>
      </a>

      <nav className="os-nav" aria-label="Main">
        {items.map((item) => {
          const count = badgeFor(item.badgeKey)
          return (
            <div key={item.path}>
              {item.startsGroup && <div className="os-nav-separator" role="separator" />}
              <NavLink
                to={item.path}
                end={item.path === '/'}
                className={({ isActive }) => `os-nav-link${isActive ? ' active' : ''}`}
                title={collapsed ? item.label : undefined}
              >
                <span className="os-nav-icon" aria-hidden="true">
                  {ICONS[item.icon] ?? '•'}
                </span>
                <span className="os-nav-label">{item.label}</span>
                {count > 0 && (
                  <Badge bg="primary" pill className="ms-auto os-nav-label">
                    {count}
                  </Badge>
                )}
              </NavLink>
            </div>
          )
        })}
      </nav>
    </aside>
  )
}
