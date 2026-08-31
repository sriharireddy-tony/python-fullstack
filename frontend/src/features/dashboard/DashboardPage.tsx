/**
 * Role-aware dashboard.
 *
 * The counts are also the substitute for notifications: with no email or Teams
 * alerts in v1, a badge on the team inbox is the only thing that makes an
 * unattended queue visible. That is a deliberate mitigation, not decoration.
 */

import { Card, Col, Row } from 'react-bootstrap'
import { Link } from 'react-router-dom'
import { useDashboardCounts, useTickets } from '@/api/hooks/useTickets'
import { PriorityBadge, RelativeTime, StatusBadge } from '@/components/badges'
import { EmptyState, ErrorState, LoadingState } from '@/components/states'
import { PageHeader } from '@/components/PageHeader'
import { Permission, ROLE_LABELS, hasPermission, type RoleValue } from '@/lib/permissions'
import { useCurrentUser, usePermissions } from '@/lib/session'

export function DashboardPage() {
  const user = useCurrentUser()
  const permissions = usePermissions()
  const { data: counts, isLoading, error, refetch } = useDashboardCounts()

  // The five most recently updated tickets the user can see — enough to notice
  // movement without turning the dashboard into a second ticket list.
  const { data: recent } = useTickets({ page: 1, page_size: 5, sort: '-updated_at' })

  const tiles = [
    {
      label: 'My open tickets',
      value: counts?.my_open,
      to: '/tickets/mine',
      show: true,
    },
    {
      label: 'Team inbox',
      value: counts?.team_inbox,
      to: '/tickets/inbox',
      show: hasPermission(permissions, Permission.TICKET_ASSIGN),
      highlight: (counts?.team_inbox ?? 0) > 0,
    },
    {
      label: 'Awaiting closure',
      value: counts?.awaiting_closure,
      to: '/tickets/awaiting',
      show: hasPermission(permissions, Permission.TICKET_CLOSE),
      highlight: (counts?.awaiting_closure ?? 0) > 0,
    },
    {
      label: 'Unassigned',
      value: counts?.unassigned,
      to: '/tickets?unassigned=true',
      show: hasPermission(permissions, Permission.TICKET_ASSIGN),
    },
  ].filter((tile) => tile.show)

  return (
    <>
      <PageHeader
        title={`Welcome, ${user.full_name.split(' ')[0]}`}
        subtitle={
          user.is_platform_admin
            ? 'Platform Administrator'
            : `${ROLE_LABELS[user.role as RoleValue]}${
                user.teams.length ? ` · ${user.teams.map((t) => t.name).join(', ')}` : ''
              }`
        }
      />

      {error && <ErrorState error={error} onRetry={() => void refetch()} />}

      <Row className="g-3 mb-4">
        {tiles.map((tile) => (
          <Col key={tile.label} md={6} xl={3}>
            <Card
              as={Link}
              to={tile.to}
              className={`h-100 text-decoration-none text-body ${
                tile.highlight ? 'border-primary' : ''
              }`}
            >
              <Card.Body>
                <div
                  className="text-secondary small text-uppercase"
                  style={{ fontSize: '0.7rem' }}
                >
                  {tile.label}
                </div>
                <div className="fs-3 fw-semibold">
                  {isLoading ? '…' : (tile.value ?? 0)}
                </div>
              </Card.Body>
            </Card>
          </Col>
        ))}
      </Row>

      <Card>
        <Card.Header className="fw-semibold small d-flex justify-content-between align-items-center">
          <span>Recent activity</span>
          <Link to="/tickets" className="small text-decoration-none">
            View all
          </Link>
        </Card.Header>
        <Card.Body className="p-0">
          {!recent && <LoadingState />}
          {recent && recent.items.length === 0 && (
            <EmptyState
              title="Nothing yet"
              description="Tickets raised by the support team will appear here."
            />
          )}
          {recent && recent.items.length > 0 && (
            <table className="table os-table mb-0 align-middle">
              <tbody>
                {recent.items.map((ticket) => (
                  <tr key={ticket.id}>
                    <td style={{ width: 90 }}>
                      <Link
                        to={`/tickets/${ticket.reference}`}
                        className="font-monospace small"
                      >
                        {ticket.reference}
                      </Link>
                    </td>
                    <td>
                      <Link to={`/tickets/${ticket.reference}`} className="text-body">
                        {ticket.title}
                      </Link>
                    </td>
                    <td style={{ width: 70 }}>
                      <PriorityBadge priority={ticket.priority} />
                    </td>
                    <td style={{ width: 110 }}>
                      <StatusBadge status={ticket.status} />
                    </td>
                    <td style={{ width: 90 }} className="small text-secondary">
                      <RelativeTime value={ticket.updated_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card.Body>
      </Card>
    </>
  )
}
