/**
 * The ticket list — the screen that matters most.
 *
 * Two decisions worth knowing:
 *
 * 1. **Filters live in the URL, not React state.** A filtered view is then
 *    bookmarkable and pasteable into chat, and survives a refresh. The team
 *    *will* send each other filtered links; that is how people use a ticket
 *    tool. Filters held only in component state make it impossible.
 *
 * 2. **Everything is server-side** — filtering, sorting, paging. Fetching all
 *    tickets and filtering in the browser works beautifully with 50 rows and
 *    dies at 5,000.
 */

import { useMemo } from 'react'
import { Badge, Button, Card, Col, Form, Pagination, Row, Table } from 'react-bootstrap'
import { Link, useSearchParams } from 'react-router-dom'
import { useTickets } from '@/api/hooks/useTickets'
import { useClients, useTeams } from '@/api/hooks/useAdmin'
import { PriorityBadge, RelativeTime, StatusBadge } from '@/components/badges'
import { EmptyState, ErrorState, LoadingState } from '@/components/states'
import { PageHeader } from '@/components/PageHeader'
import { Permission, hasPermission } from '@/lib/permissions'
import { usePermissions } from '@/lib/session'
import type { Priority, TicketFilters, TicketStatus } from '@/api/types'

const STATUS_OPTIONS: TicketStatus[] = [
  'open',
  'assigned',
  'in_progress',
  'on_hold',
  'resolved',
  'rejected',
  'closed',
]

const PRIORITY_OPTIONS: Priority[] = ['p1', 'p2', 'p3', 'p4']

export interface TicketListPageProps {
  /** Saved-view preset applied on top of the URL filters. */
  preset?: 'mine' | 'inbox' | 'awaiting' | 'unassigned'
  title?: string
  subtitle?: string
}

export function TicketListPage({ preset, title, subtitle }: TicketListPageProps) {
  const [params, setParams] = useSearchParams()
  const permissions = usePermissions()
  const { data: teams } = useTeams(true)
  const { data: clients } = useClients({ activeOnly: true })

  const filters = useMemo<TicketFilters>(() => {
    const base: TicketFilters = {
      page: Number(params.get('page') ?? 1),
      page_size: 25,
      sort: params.get('sort') ?? '-created_at',
    }
    const status = params.getAll('status') as TicketStatus[]
    const priority = params.getAll('priority') as Priority[]
    if (status.length) base.status = status
    if (priority.length) base.priority = priority
    if (params.get('team_id')) base.team_id = [params.get('team_id') as string]
    if (params.get('client_id')) base.client_id = [params.get('client_id') as string]
    if (params.get('q')) base.q = params.get('q') as string

    // Presets are applied last so a saved view cannot be silently overridden
    // by a stale query parameter.
    if (preset === 'mine') base.assignee_id = 'me'
    if (preset === 'inbox') base.unassigned = true
    if (preset === 'unassigned') base.unassigned = true
    if (preset === 'awaiting') base.awaiting_closure = true

    return base
  }, [params, preset])

  const { data, isLoading, error, refetch, isFetching } = useTickets(filters)

  function setParam(key: string, value: string | null) {
    const next = new URLSearchParams(params)
    if (value === null || value === '') next.delete(key)
    else next.set(key, value)
    next.delete('page') // a filter change invalidates the current page number
    setParams(next)
  }

  function toggleMulti(key: string, value: string) {
    const next = new URLSearchParams(params)
    const current = next.getAll(key)
    next.delete(key)
    const updated = current.includes(value)
      ? current.filter((v) => v !== value)
      : [...current, value]
    updated.forEach((v) => next.append(key, v))
    next.delete('page')
    setParams(next)
  }

  const activeStatuses = params.getAll('status')
  const activePriorities = params.getAll('priority')
  const hasFilters = Array.from(params.keys()).some((k) => k !== 'page' && k !== 'sort')

  return (
    <>
      <PageHeader
        title={title ?? 'All Tickets'}
        subtitle={subtitle}
        actions={
          hasPermission(permissions, Permission.TICKET_CREATE) ? (
            <Link to="/tickets/new" className="btn btn-primary btn-sm">
              + New Ticket
            </Link>
          ) : undefined
        }
      />

      <Card className="mb-3">
        <Card.Body className="py-3">
          <Row className="g-2 align-items-end">
            <Col md={4}>
              <Form.Label className="small fw-medium mb-1">Search</Form.Label>
              <Form.Control
                size="sm"
                type="search"
                placeholder="Text, or a ticket number"
                defaultValue={params.get('q') ?? ''}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') setParam('q', e.currentTarget.value)
                }}
                onBlur={(e) => setParam('q', e.currentTarget.value)}
              />
            </Col>
            <Col md={3}>
              <Form.Label className="small fw-medium mb-1">Team</Form.Label>
              <Form.Select
                size="sm"
                value={params.get('team_id') ?? ''}
                onChange={(e) => setParam('team_id', e.target.value)}
              >
                <option value="">All teams</option>
                {teams?.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </Form.Select>
            </Col>
            <Col md={3}>
              <Form.Label className="small fw-medium mb-1">Client</Form.Label>
              <Form.Select
                size="sm"
                value={params.get('client_id') ?? ''}
                onChange={(e) => setParam('client_id', e.target.value)}
              >
                <option value="">All clients</option>
                {clients?.items.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </Form.Select>
            </Col>
            <Col md={2}>
              <Form.Label className="small fw-medium mb-1">Sort</Form.Label>
              <Form.Select
                size="sm"
                value={params.get('sort') ?? '-created_at'}
                onChange={(e) => setParam('sort', e.target.value)}
              >
                <option value="-created_at">Newest first</option>
                <option value="created_at">Oldest first</option>
                <option value="-updated_at">Recently updated</option>
                <option value="priority">Priority</option>
              </Form.Select>
            </Col>
          </Row>

          <div className="d-flex flex-wrap gap-3 mt-3 align-items-center">
            <FilterChips
              label="Status"
              options={STATUS_OPTIONS}
              active={activeStatuses}
              onToggle={(v) => toggleMulti('status', v)}
              render={(v) => <StatusBadge status={v as TicketStatus} />}
            />
            <FilterChips
              label="Priority"
              options={PRIORITY_OPTIONS}
              active={activePriorities}
              onToggle={(v) => toggleMulti('priority', v)}
              render={(v) => <PriorityBadge priority={v as Priority} />}
            />
            {hasFilters && (
              <Button size="sm" variant="link" className="ms-auto" onClick={() => setParams({})}>
                Clear filters
              </Button>
            )}
          </div>
        </Card.Body>
      </Card>

      {isLoading && <LoadingState label="Loading tickets…" />}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}

      {data && data.items.length === 0 && (
        <EmptyState
          title="No tickets match"
          description={
            hasFilters
              ? 'Try widening or clearing the filters.'
              : 'Nothing here yet. Tickets raised by the support team will appear in this list.'
          }
          action={
            hasFilters ? (
              <Button size="sm" variant="outline-secondary" onClick={() => setParams({})}>
                Clear filters
              </Button>
            ) : undefined
          }
        />
      )}

      {data && data.items.length > 0 && (
        <Card>
          <Table hover responsive className="os-table mb-0 align-middle">
            <thead>
              <tr>
                <th style={{ width: 90 }}>#</th>
                <th>Title</th>
                <th style={{ width: 140 }}>Client</th>
                <th style={{ width: 110 }}>Team</th>
                <th style={{ width: 140 }}>Assignee</th>
                <th style={{ width: 70 }}>Priority</th>
                <th style={{ width: 110 }}>Status</th>
                <th style={{ width: 90 }}>Updated</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((ticket) => (
                <tr key={ticket.id}>
                  <td>
                    <Link to={`/tickets/${ticket.reference}`} className="font-monospace small">
                      {ticket.reference}
                    </Link>
                  </td>
                  <td>
                    <Link to={`/tickets/${ticket.reference}`} className="text-body">
                      {ticket.title}
                    </Link>
                    {ticket.reopen_count > 0 && (
                      <Badge bg="warning" className="ms-2" title="Reopened">
                        ↻ {ticket.reopen_count}
                      </Badge>
                    )}
                  </td>
                  <td className="small">{ticket.client?.name ?? '—'}</td>
                  <td className="small">{ticket.team?.name ?? '—'}</td>
                  <td className="small">
                    {ticket.assignee?.full_name ?? (
                      <span className="text-secondary fst-italic">Unassigned</span>
                    )}
                  </td>
                  <td>
                    <PriorityBadge priority={ticket.priority} />
                  </td>
                  <td>
                    <StatusBadge status={ticket.status} />
                  </td>
                  <td className="small text-secondary">
                    <RelativeTime value={ticket.updated_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>

          <Card.Footer className="d-flex align-items-center justify-content-between py-2">
            <span className="small text-secondary">
              {data.total} ticket{data.total === 1 ? '' : 's'}
              {isFetching && ' · updating…'}
            </span>
            <Pager
              page={data.page}
              pageSize={data.page_size}
              total={data.total}
              onChange={(p) => setParam('page', String(p))}
            />
          </Card.Footer>
        </Card>
      )}
    </>
  )
}

function FilterChips({
  label,
  options,
  active,
  onToggle,
  render,
}: {
  label: string
  options: string[]
  active: string[]
  onToggle: (value: string) => void
  render: (value: string) => React.ReactNode
}) {
  return (
    <div className="d-flex align-items-center gap-2">
      <span className="small text-secondary">{label}:</span>
      {options.map((option) => (
        <button
          key={option}
          type="button"
          className="btn btn-link p-0 border-0"
          style={{ opacity: active.length === 0 || active.includes(option) ? 1 : 0.35 }}
          onClick={() => onToggle(option)}
          aria-pressed={active.includes(option)}
        >
          {render(option)}
        </button>
      ))}
    </div>
  )
}

function Pager({
  page,
  pageSize,
  total,
  onChange,
}: {
  page: number
  pageSize: number
  total: number
  onChange: (page: number) => void
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  if (pages <= 1) return null

  const window = 2
  const numbers = Array.from({ length: pages }, (_, i) => i + 1).filter(
    (n) => n === 1 || n === pages || Math.abs(n - page) <= window,
  )

  return (
    <Pagination size="sm" className="mb-0">
      <Pagination.Prev disabled={page <= 1} onClick={() => onChange(page - 1)} />
      {numbers.map((n, index) => (
        <span key={n} className="d-flex">
          {index > 0 && n - (numbers[index - 1] as number) > 1 && <Pagination.Ellipsis disabled />}
          <Pagination.Item active={n === page} onClick={() => onChange(n)}>
            {n}
          </Pagination.Item>
        </span>
      ))}
      <Pagination.Next disabled={page >= pages} onClick={() => onChange(page + 1)} />
    </Pagination>
  )
}
