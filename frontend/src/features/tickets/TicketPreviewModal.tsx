import { Badge, Button, Col, Modal, Row } from 'react-bootstrap'
import { Link } from 'react-router-dom'
import { useTicket } from '@/api/hooks/useTickets'
import { PriorityBadge, RelativeTime, StatusBadge } from '@/components/badges'
import { EmptyState, ErrorState, LoadingState } from '@/components/states'
import type { TicketDetail } from '@/api/types'

/**
 * A read-only preview of another ticket, opened from the Similar Issues panel.
 *
 * ## Why a modal and not a navigation
 *
 * Following a link means losing the ticket you were reading. The whole point of
 * the similar-issues panel is comparison — "is this the same bug as the one in
 * front of me?" — and you cannot compare two things when looking at one costs
 * you the other. The modal keeps the original ticket underneath, one Escape
 * away.
 *
 * ## Why it has no Similar Issues panel of its own
 *
 * Because it would recurse. Ticket A suggests B, B suggests C, and a reader
 * three modals deep has forgotten what they were originally looking at — and
 * every level costs a retrieval round-trip and possibly an LLM call. The panel
 * belongs to the ticket you are *working on*, and there is exactly one of those.
 *
 * The Analysis tab is absent for the same reason, plus a sharper one: analysis
 * is an on-demand agent run costing real money, and it must be started
 * deliberately from the ticket's own page rather than as a side effect of
 * glancing at a suggestion.
 *
 * ## Read-only on purpose
 *
 * No actions, no comment box, no status changes. This is a *reference* to
 * something the reader is comparing against, and offering to mutate a ticket
 * from a preview invites acting on the wrong one. "Open full ticket" is the
 * route to anything that changes state, and it is deliberately a real
 * navigation.
 */
export function TicketPreviewModal({
  reference,
  onHide,
}: {
  reference: string | null
  onHide: () => void
}) {
  return (
    <Modal show={reference !== null} onHide={onHide} size="lg" scrollable centered>
      {reference !== null && <PreviewBody reference={reference} onHide={onHide} />}
    </Modal>
  )
}

function PreviewBody({ reference, onHide }: { reference: string; onHide: () => void }) {
  const { data: ticket, isLoading, error, refetch } = useTicket(reference)

  return (
    <>
      <Modal.Header closeButton>
        <div>
          <div className="d-flex align-items-center gap-2 mb-1">
            <span className="font-monospace text-secondary small">{reference}</span>
            {ticket && (
              <>
                <PriorityBadge
                  priority={ticket.priority}
                  overridden={ticket.priority_overridden}
                />
                <StatusBadge status={ticket.status} />
                {ticket.reopen_count > 0 && (
                  <Badge bg="warning">Reopened {ticket.reopen_count}×</Badge>
                )}
              </>
            )}
          </div>
          <Modal.Title as="h5" className="mb-0">
            {ticket?.title ?? 'Loading…'}
          </Modal.Title>
        </div>
      </Modal.Header>

      <Modal.Body>
        {isLoading && <LoadingState label="Loading ticket…" />}
        {error && <ErrorState error={error} onRetry={() => void refetch()} />}
        {!isLoading && !error && !ticket && <EmptyState title="Ticket not found" />}
        {ticket && <PreviewContent ticket={ticket} />}
      </Modal.Body>

      <Modal.Footer className="justify-content-between">
        <div className="text-secondary" style={{ fontSize: '0.75rem' }}>
          Read-only preview. Open the full ticket to comment or change its status.
        </div>
        <div className="d-flex gap-2">
          <Button variant="outline-secondary" size="sm" onClick={onHide}>
            Close
          </Button>
          <Link to={`/tickets/${reference}`} className="btn btn-primary btn-sm">
            Open full ticket
          </Link>
        </div>
      </Modal.Footer>
    </>
  )
}

function PreviewContent({ ticket }: { ticket: TicketDetail }) {
  return (
    <>
      {ticket.resolution_notes && (
        // First, not last. A reader opening a closed ticket from the similar
        // panel is almost always looking for one thing: how it was fixed.
        <div className="alert alert-success py-2">
          <div className="fw-semibold small mb-1">Resolution</div>
          <div style={{ whiteSpace: 'pre-wrap' }}>{ticket.resolution_notes}</div>
        </div>
      )}

      {ticket.rejection_reason && (
        <div className="alert alert-dark py-2">
          <div className="fw-semibold small mb-1">
            Rejected — {ticket.rejection_reason.replace(/_/g, ' ')}
          </div>
        </div>
      )}

      <Section title="Description" body={ticket.description} />
      <Section title="Steps to reproduce" body={ticket.steps_to_reproduce} />
      <Row>
        <Col md={6}>
          <Section title="Expected result" body={ticket.expected_result} />
        </Col>
        <Col md={6}>
          <Section title="Actual result" body={ticket.actual_result} />
        </Col>
      </Row>

      <hr />

      <Row className="small">
        <Col md={6}>
          <Meta label="Client" value={ticket.client?.name} />
          <Meta label="Team" value={ticket.team?.name} />
          <Meta label="Assignee" value={ticket.assignee?.full_name ?? 'Unassigned'} />
        </Col>
        <Col md={6}>
          <Meta label="Severity" value={ticket.severity.replace(/_/g, ' ')} />
          <Meta label="Impact" value={ticket.impact.replace(/_/g, ' ')} />
          <Meta label="Raised" value={<RelativeTime value={ticket.created_at} />} />
          {ticket.closed_at && (
            <Meta label="Closed" value={<RelativeTime value={ticket.closed_at} />} />
          )}
        </Col>
      </Row>
    </>
  )
}

function Section({ title, body }: { title: string; body: string | null }) {
  if (!body) return null
  return (
    <div className="mb-3">
      <div className="text-secondary small text-uppercase mb-1" style={{ fontSize: '0.7rem' }}>
        {title}
      </div>
      {/* Plain text, never dangerouslySetInnerHTML — this is user-supplied
          content and React's escaping is what keeps it safe. */}
      <div style={{ whiteSpace: 'pre-wrap' }}>{body}</div>
    </div>
  )
}

function Meta({ label, value }: { label: string; value: React.ReactNode }) {
  if (!value) return null
  return (
    <div className="d-flex justify-content-between gap-2 py-1">
      <span className="text-secondary">{label}</span>
      <span className="text-end">{value}</span>
    </div>
  )
}
