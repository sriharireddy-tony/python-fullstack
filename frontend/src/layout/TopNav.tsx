import { Button, Form } from 'react-bootstrap'
import { useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { Permission, hasPermission } from '@/lib/permissions'
import { usePermissions } from '@/lib/session'
import { UserMenu } from './UserMenu'

interface TopNavProps {
  collapsed: boolean
  onToggleSidebar: () => void
}

export function TopNav({ collapsed, onToggleSidebar }: TopNavProps) {
  const permissions = usePermissions()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')

  const canCreate = hasPermission(permissions, Permission.TICKET_CREATE)

  function onSearch(event: React.FormEvent) {
    event.preventDefault()
    const term = query.trim()
    if (!term) return

    // A bare number, or OS-1042, jumps straight to the ticket. Anything else
    // becomes a search - people quote ticket numbers far more often than they
    // search prose.
    const match = /^(?:os-)?(\d+)$/i.exec(term)
    if (match) navigate(`/tickets/OS-${match[1]}`)
    else navigate(`/tickets?q=${encodeURIComponent(term)}`)
  }

  return (
    <header className="os-topnav">
      <Button
        variant="link"
        className="text-body px-2"
        onClick={onToggleSidebar}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        aria-expanded={!collapsed}
      >
        ☰
      </Button>

      <Form className="os-search" onSubmit={onSearch} role="search">
        <Form.Control
          type="search"
          size="sm"
          placeholder="Search tickets, or jump to OS-1042…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search tickets"
        />
      </Form>

      <div className="ms-auto d-flex align-items-center gap-2">
        {canCreate && (
          <Button size="sm" variant="primary" onClick={() => navigate('/tickets/new')}>
            + New Ticket
          </Button>
        )}
        <UserMenu />
      </div>
    </header>
  )
}
