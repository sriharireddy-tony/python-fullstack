import { useState } from 'react'
import { Badge, Button, Card, Col, Form, Nav, Row, Spinner } from 'react-bootstrap'
import { useParams } from 'react-router-dom'
import {
  useAddComment,
  useAttachments,
  useComments,
  useDeleteAttachment,
  useDeleteComment,
  useTicket,
  useTicketHistory,
  useUploadAttachment,
} from '@/api/hooks/useTickets'
import { PriorityBadge, RelativeTime, StatusBadge, formatBytes, statusLabel } from '@/components/badges'
import { EmptyState, ErrorState, LoadingState } from '@/components/states'
import { TicketActions } from './TicketActions'
import type { TicketDetail } from '@/api/types'

type Tab = 'details' | 'conversation' | 'attachments' | 'history'

export function TicketDetailPage() {
  const { ticketRef } = useParams<{ ticketRef: string }>()
  const [tab, setTab] = useState<Tab>('details')
  const { data: ticket, isLoading, error, refetch } = useTicket(ticketRef)
  const { data: comments } = useComments(ticket?.id)
  const { data: attachments } = useAttachments(ticket?.id)

  if (isLoading) return <LoadingState label="Loading ticket…" />
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />
  if (!ticket) return <EmptyState title="Ticket not found" />

  return (
    <>
      <div className="d-flex align-items-start justify-content-between gap-3 mb-1">
        <div>
          <div className="d-flex align-items-center gap-2 mb-1">
            <span className="font-monospace text-secondary small">{ticket.reference}</span>
            <PriorityBadge priority={ticket.priority} overridden={ticket.priority_overridden} />
            <StatusBadge status={ticket.status} />
            {ticket.reopen_count > 0 && (
              <Badge bg="warning">Reopened {ticket.reopen_count}×</Badge>
            )}
          </div>
          <h1 className="os-page-title">{ticket.title}</h1>
        </div>
      </div>

      {ticket.status === 'on_hold' && ticket.on_hold_waiting_on && (
        <div className="alert alert-secondary py-2 small">
          On hold — waiting on <strong>{ticket.on_hold_waiting_on.replace('_', ' ')}</strong>
        </div>
      )}

      <Row className="g-3 mt-1">
        <Col lg={8}>
          <Card>
            <Card.Header className="pb-0 border-0">
              <Nav variant="tabs" activeKey={tab} onSelect={(k) => setTab((k as Tab) ?? 'details')}>
                <Nav.Item>
                  <Nav.Link eventKey="details">Details</Nav.Link>
                </Nav.Item>
                <Nav.Item>
                  <Nav.Link eventKey="conversation">
                    Conversation {comments?.length ? `(${comments.length})` : ''}
                  </Nav.Link>
                </Nav.Item>
                <Nav.Item>
                  <Nav.Link eventKey="attachments">
                    Attachments {attachments?.length ? `(${attachments.length})` : ''}
                  </Nav.Link>
                </Nav.Item>
                <Nav.Item>
                  <Nav.Link eventKey="history">History</Nav.Link>
                </Nav.Item>
              </Nav>
            </Card.Header>

            <Card.Body>
              {tab === 'details' && <DetailsTab ticket={ticket} />}
              {tab === 'conversation' && <ConversationTab ticket={ticket} />}
              {tab === 'attachments' && <AttachmentsTab ticket={ticket} />}
              {tab === 'history' && <HistoryTab ticketId={ticket.id} />}
            </Card.Body>
          </Card>
        </Col>

        <Col lg={4}>
          <Card className="mb-3">
            <Card.Header className="fw-semibold small">Actions</Card.Header>
            <Card.Body>
              <TicketActions ticket={ticket} />
            </Card.Body>
          </Card>

          <Card>
            <Card.Header className="fw-semibold small">Details</Card.Header>
            <Card.Body className="small">
              <Meta label="Client" value={ticket.client?.name} />
              <Meta label="Reported by" value={ticket.reporter_name} />
              <Meta label="Contact" value={ticket.reporter_email} />
              <hr className="my-2" />
              <Meta label="Team" value={ticket.team?.name} />
              <Meta
                label="Assignee"
                value={ticket.assignee?.full_name ?? 'Unassigned'}
                muted={!ticket.assignee}
              />
              <Meta label="Raised by" value={ticket.created_by?.full_name} />
              <hr className="my-2" />
              <Meta label="Severity" value={ticket.severity.replace('_', ' ')} />
              <Meta label="Impact" value={ticket.impact.replace('_', ' ')} />
              <Meta label="Workaround" value={ticket.workaround} />
              <Meta label="Environment" value={ticket.environment} />
              {ticket.priority_overridden && (
                <div className="alert alert-warning py-2 px-2 my-2 small">
                  <strong>Priority overridden.</strong> {ticket.priority_override_reason}
                </div>
              )}
              <hr className="my-2" />
              {ticket.ado_work_item_url && (
                <Meta
                  label="ADO item"
                  value={
                    <a href={ticket.ado_work_item_url} target="_blank" rel="noreferrer noopener">
                      Open in Azure DevOps
                    </a>
                  }
                />
              )}
              <Meta label="Created" value={<RelativeTime value={ticket.created_at} />} />
              <Meta label="Updated" value={<RelativeTime value={ticket.updated_at} />} />
              {ticket.resolved_at && (
                <Meta label="Resolved" value={<RelativeTime value={ticket.resolved_at} />} />
              )}
              {ticket.closed_at && (
                <Meta label="Closed" value={<RelativeTime value={ticket.closed_at} />} />
              )}
            </Card.Body>
          </Card>
        </Col>
      </Row>
    </>
  )
}

function Meta({
  label,
  value,
  muted,
}: {
  label: string
  value: React.ReactNode
  muted?: boolean
}) {
  if (!value) return null
  return (
    <div className="d-flex justify-content-between gap-2 py-1">
      <span className="text-secondary">{label}</span>
      <span className={`text-end ${muted ? 'text-secondary fst-italic' : ''}`}>{value}</span>
    </div>
  )
}

function DetailsTab({ ticket }: { ticket: TicketDetail }) {
  return (
    <>
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
      {ticket.resolution_notes && (
        <div className="alert alert-success">
          <div className="fw-semibold small mb-1">Resolution</div>
          <div style={{ whiteSpace: 'pre-wrap' }}>{ticket.resolution_notes}</div>
        </div>
      )}
      {ticket.rejection_reason && (
        <div className="alert alert-dark">
          <div className="fw-semibold small mb-1">
            Rejected — {ticket.rejection_reason.replace(/_/g, ' ')}
          </div>
          <div className="small">See the conversation for the full explanation.</div>
        </div>
      )}
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

function ConversationTab({ ticket }: { ticket: TicketDetail }) {
  const { data: comments, isLoading } = useComments(ticket.id)
  const addComment = useAddComment(ticket.id)
  const deleteComment = useDeleteComment(ticket.id)
  const [draft, setDraft] = useState('')

  async function submit() {
    if (!draft.trim()) return
    await addComment.mutateAsync(draft)
    setDraft('')
  }

  return (
    <>
      {isLoading && <LoadingState />}

      {comments?.length === 0 && (
        <EmptyState
          title="No comments yet"
          description="This is where a rejection gets explained and agreed before CS closes the ticket."
        />
      )}

      <div className="d-flex flex-column gap-3 mb-4">
        {comments?.map((comment) => (
          <div key={comment.id} className="border-start border-3 ps-3">
            <div className="d-flex align-items-center gap-2 mb-1">
              <span className="fw-semibold small">{comment.author?.full_name ?? 'Unknown'}</span>
              <span className="text-secondary" style={{ fontSize: '0.75rem' }}>
                <RelativeTime value={comment.created_at} />
                {comment.edited_at && ' · edited'}
              </span>
              {comment.can_edit && (
                <Button
                  variant="link"
                  size="sm"
                  className="p-0 ms-auto text-danger"
                  style={{ fontSize: '0.75rem' }}
                  onClick={() => void deleteComment.mutateAsync(comment.id)}
                >
                  Delete
                </Button>
              )}
            </div>
            <div style={{ whiteSpace: 'pre-wrap' }}>{comment.body}</div>
          </div>
        ))}
      </div>

      <Form.Group>
        <Form.Control
          as="textarea"
          rows={3}
          placeholder="Add a comment…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <div className="d-flex justify-content-end mt-2">
          <Button size="sm" onClick={submit} disabled={!draft.trim() || addComment.isPending}>
            {addComment.isPending ? 'Posting…' : 'Comment'}
          </Button>
        </div>
      </Form.Group>
    </>
  )
}

function AttachmentsTab({ ticket }: { ticket: TicketDetail }) {
  const { data: attachments, isLoading } = useAttachments(ticket.id)
  const upload = useUploadAttachment(ticket.id)
  const remove = useDeleteAttachment(ticket.id)

  return (
    <>
      {isLoading && <LoadingState />}

      {attachments?.length === 0 && (
        <EmptyState title="No files attached" description="Screenshots and logs go here." />
      )}

      <div className="d-flex flex-column gap-2 mb-3">
        {attachments?.map((file) => (
          <div
            key={file.id}
            className="d-flex align-items-center gap-3 border rounded p-2"
          >
            <span aria-hidden="true">📎</span>
            <div className="flex-grow-1">
              {/* Downloads stream through a permission-checked endpoint; the
                  upload directory is never served statically. */}
              <a href={`/api/v1/attachments/${file.id}/download`} className="small">
                {file.original_filename}
              </a>
              <div className="text-secondary" style={{ fontSize: '0.75rem' }}>
                {formatBytes(file.size_bytes)} · <RelativeTime value={file.created_at} />
              </div>
            </div>
            <Button
              variant="link"
              size="sm"
              className="text-danger p-0"
              onClick={() => void remove.mutateAsync(file.id)}
            >
              Remove
            </Button>
          </div>
        ))}
      </div>

      {upload.isError && (
        <div className="alert alert-danger py-2 small">{(upload.error as Error).message}</div>
      )}

      <Form.Group>
        <Form.Label className="small fw-medium">Add a file</Form.Label>
        <Form.Control
          type="file"
          size="sm"
          disabled={upload.isPending}
          onChange={(e) => {
            const input = e.target as HTMLInputElement
            const file = input.files?.[0]
            if (file) void upload.mutateAsync(file).finally(() => (input.value = ''))
          }}
        />
        <Form.Text className="text-secondary">
          Images, PDF, logs and CSV. Up to 10 MB, 5 files per ticket.
        </Form.Text>
      </Form.Group>
      {upload.isPending && (
        <div className="small text-secondary mt-2">
          <Spinner animation="border" size="sm" className="me-2" />
          Uploading…
        </div>
      )}
    </>
  )
}

function HistoryTab({ ticketId }: { ticketId: string }) {
  const { data: history, isLoading } = useTicketHistory(ticketId)

  if (isLoading) return <LoadingState />
  if (!history?.length) return <EmptyState title="No history yet" />

  return (
    <div className="d-flex flex-column gap-2">
      {history.map((entry) => (
        <div key={entry.id} className="d-flex gap-3 align-items-start">
          <div className="text-secondary small text-nowrap" style={{ width: 90 }}>
            <RelativeTime value={entry.changed_at} />
          </div>
          <div className="small">
            {entry.from_status ? (
              <>
                <span className="text-secondary">{statusLabel(entry.from_status)}</span>
                {' → '}
                <strong>{statusLabel(entry.to_status)}</strong>
              </>
            ) : (
              <strong>Created</strong>
            )}
            {entry.changed_by && (
              <span className="text-secondary"> by {entry.changed_by.full_name}</span>
            )}
            {entry.note && <div className="text-secondary">{entry.note}</div>}
          </div>
        </div>
      ))}
    </div>
  )
}
