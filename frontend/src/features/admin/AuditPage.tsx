import { useState } from 'react'
import { Badge, Card, Form, Table } from 'react-bootstrap'
import { useAuditLog } from '@/api/hooks/useAdmin'
import { RelativeTime } from '@/components/badges'
import { EmptyState, ErrorState, LoadingState } from '@/components/states'
import { PageHeader } from '@/components/PageHeader'

const ENTITY_TYPES = ['ticket', 'user', 'team', 'client', 'comment', 'attachment']

export function AuditPage() {
  const [entityType, setEntityType] = useState('')
  const { data, isLoading, error, refetch } = useAuditLog({
    entityType: entityType || undefined,
  })

  return (
    <>
      <PageHeader
        title="Audit Log"
        subtitle="Append-only record of every change. Never edited, never deleted."
      />

      <Card className="mb-3">
        <Card.Body className="py-2">
          <Form.Group className="d-flex align-items-center gap-2">
            <Form.Label className="small fw-medium mb-0">Entity</Form.Label>
            <Form.Select
              size="sm"
              style={{ maxWidth: 200 }}
              value={entityType}
              onChange={(e) => setEntityType(e.target.value)}
            >
              <option value="">Everything</option>
              {ENTITY_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Form.Select>
          </Form.Group>
        </Card.Body>
      </Card>

      {isLoading && <LoadingState />}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}
      {data && data.items.length === 0 && <EmptyState title="No entries" />}

      {data && data.items.length > 0 && (
        <Card>
          <Table hover responsive className="os-table mb-0 align-middle">
            <thead>
              <tr>
                <th style={{ width: 110 }}>When</th>
                <th style={{ width: 200 }}>Action</th>
                <th style={{ width: 110 }}>Entity</th>
                <th>Change</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((entry) => (
                <tr key={entry.id}>
                  <td className="small text-secondary">
                    <RelativeTime value={entry.created_at} />
                  </td>
                  <td>
                    <Badge bg="light" text="dark" className="font-monospace">
                      {entry.action}
                    </Badge>
                  </td>
                  <td className="small">{entry.entity_type}</td>
                  <td className="small text-secondary">
                    {/* Ticket and comment bodies are redacted server-side
                        before they ever reach this table — this log is visible
                        to admins and the content is HR support data. */}
                    <code style={{ fontSize: '0.75rem' }}>
                      {summarise(entry.changes)}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
          <Card.Footer className="small text-secondary py-2">
            {data.total} entr{data.total === 1 ? 'y' : 'ies'}
          </Card.Footer>
        </Card>
      )}
    </>
  )
}

function summarise(changes: Record<string, unknown>): string {
  const after = changes.after as Record<string, unknown> | undefined
  const before = changes.before as Record<string, unknown> | undefined
  if (!after && !before) return '—'

  const keys = Object.keys(after ?? before ?? {})
  return keys
    .slice(0, 4)
    .map((key) => {
      const from = before?.[key]
      const to = after?.[key]
      if (from !== undefined && to !== undefined) return `${key}: ${from} → ${to}`
      return `${key}: ${to ?? from}`
    })
    .join(' · ')
}
