import { useMemo, useState } from 'react'
import { Alert, Badge, Button, Card, Col, Form, Row, Spinner, Table } from 'react-bootstrap'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState } from '@/components/states'
import type { EmbeddingRow, EmbeddingState } from '@/api/types'
import {
  useDeleteEmbeddings,
  useEmbedTickets,
  useEmbeddingList,
  useEmbeddingSummary,
} from '../api/useEmbeddings'

/**
 * The embedding console.
 *
 * Operates the vector index: shows what is indexed, embeds what is not, and
 * deletes what should not be. Gated on `embedding:manage`, which team managers
 * and tenant admins hold.
 *
 * ## Why this page exists at all
 *
 * Embedding is normally automatic — a ticket is written, an outbox row is
 * queued, a worker embeds it. That is the right production default and it is
 * what `AI_AUTO_EMBED_ON_WRITE` turns back on. But automatic work is invisible
 * work, and three jobs stay human even when it is on: backfilling a corpus
 * that predates the AI layer, repairing drift after a failure, and re-embedding
 * everything after a model change. This page is for those.
 *
 * ## The state that matters most
 *
 * `stale` is the interesting one. A stale ticket *has* a vector, so it looks
 * indexed, but its text changed after that vector was written — so retrieval
 * will confidently return it for the wrong reasons. That is worse than having
 * no vector at all, which is why it gets its own colour rather than being
 * folded into "embedded".
 */

const STATE_META: Record<EmbeddingState, { label: string; bg: string; hint: string }> = {
  embedded: {
    label: 'Embedded',
    bg: 'success',
    hint: 'A vector exists and matches the current text.',
  },
  stale: {
    label: 'Stale',
    bg: 'warning',
    hint: 'A vector exists, but the ticket text changed after it was written.',
  },
  not_embedded: {
    label: 'Not embedded',
    bg: 'secondary',
    hint: 'No vector has been written for this ticket.',
  },
}

export function EmbeddingsPage() {
  const [stateFilter, setStateFilter] = useState<EmbeddingState | ''>('')
  const [statusFilter, setStatusFilter] = useState('')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [force, setForce] = useState(false)

  const filters = useMemo(
    () => ({
      state: stateFilter || undefined,
      status: statusFilter || undefined,
      search: search.trim() || undefined,
      limit: 200,
    }),
    [stateFilter, statusFilter, search],
  )

  const list = useEmbeddingList(filters)
  const summary = useEmbeddingSummary()
  const embed = useEmbedTickets()
  const remove = useDeleteEmbeddings()

  const rows = list.data?.rows ?? []
  const selectedIds = [...selected]
  const busy = embed.isPending || remove.isPending

  function toggle(ticketId: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(ticketId)) next.delete(ticketId)
      else next.add(ticketId)
      return next
    })
  }

  function toggleAll() {
    // Selects only what is currently visible. Selecting rows behind a filter
    // the operator cannot see is how a bulk action does something surprising.
    setSelected((current) =>
      current.size === rows.length ? new Set() : new Set(rows.map((r) => r.ticket_id)),
    )
  }

  async function runEmbed() {
    if (!selectedIds.length) return
    await embed.mutateAsync({ ticketIds: selectedIds, force })
    setSelected(new Set())
  }

  async function runDelete() {
    if (!selectedIds.length) return
    await remove.mutateAsync(selectedIds)
    setSelected(new Set())
  }

  return (
    <div>
      <PageHeader
        title="Embeddings"
        subtitle="What is in the vector index, and the controls to change it."
      />

      {summary.isError ? (
        <Alert variant="warning" className="mb-3">
          Could not read the index summary. The vector store or the embedding model may be
          unreachable — the table below still shows what Postgres has recorded.
        </Alert>
      ) : null}

      {summary.data ? (
        <>
          <Row className="g-3 mb-3">
            <Stat label="Tickets" value={summary.data.total} />
            <Stat label="Embedded" value={summary.data.embedded} bg="success" />
            <Stat label="Stale" value={summary.data.stale} bg="warning" />
            <Stat label="Not embedded" value={summary.data.not_embedded} bg="secondary" />
            <Stat
              label="Vectors in store"
              value={
                summary.data.vectors_in_store < 0 ? 'unreachable' : summary.data.vectors_in_store
              }
              bg="info"
              hint="Counted from Pinecone itself, by listing the tenant's vector ids. When this disagrees with Embedded, the two stores have drifted."
            />
          </Row>

          <p className="text-muted small mb-3">
            Model <code>{summary.data.model}</code>
            {summary.data.dimension ? (
              <>
                {' '}
                at <code>{summary.data.dimension}</code> dimensions
              </>
            ) : null}
            .{' '}
            {summary.data.auto_embed_on_write ? (
              <>Automatic embedding on write is <strong>on</strong> — new tickets index themselves.</>
            ) : (
              <>
                Automatic embedding on write is <strong>off</strong>, so nothing is embedded until
                you do it here.
              </>
            )}
          </p>
        </>
      ) : null}

      <Card className="mb-3">
        <Card.Body>
          <Row className="g-2 align-items-end">
            <Col md={3}>
              <Form.Label className="small mb-1">Index state</Form.Label>
              <Form.Select
                size="sm"
                value={stateFilter}
                onChange={(e) => setStateFilter(e.target.value as EmbeddingState | '')}
              >
                <option value="">All</option>
                <option value="not_embedded">Not embedded</option>
                <option value="embedded">Embedded</option>
                <option value="stale">Stale</option>
              </Form.Select>
            </Col>
            <Col md={3}>
              <Form.Label className="small mb-1">Ticket status</Form.Label>
              <Form.Select
                size="sm"
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
              >
                <option value="">All</option>
                <option value="open">Open</option>
                <option value="assigned">Assigned</option>
                <option value="in_progress">In progress</option>
                <option value="on_hold">On hold</option>
                <option value="resolved">Resolved</option>
                <option value="closed">Closed</option>
                <option value="rejected">Rejected</option>
              </Form.Select>
            </Col>
            <Col md={6}>
              <Form.Label className="small mb-1">Search</Form.Label>
              <Form.Control
                size="sm"
                placeholder="Title or description…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </Col>
          </Row>
        </Card.Body>
      </Card>

      <Card className="mb-3">
        <Card.Body className="d-flex flex-wrap gap-2 align-items-center">
          <Button size="sm" variant="primary" disabled={!selectedIds.length || busy} onClick={runEmbed}>
            {embed.isPending ? (
              <>
                <Spinner size="sm" animation="border" className="me-2" />
                Embedding…
              </>
            ) : (
              <>Embed {selectedIds.length || ''}</>
            )}
          </Button>

          <Button
            size="sm"
            variant="outline-danger"
            disabled={!selectedIds.length || busy}
            onClick={runDelete}
          >
            Delete embeddings
          </Button>

          <Form.Check
            type="checkbox"
            id="force-reembed"
            className="ms-2"
            label="Overwrite existing"
            checked={force}
            onChange={(e) => setForce(e.target.checked)}
          />
          <span className="text-muted small">
            Without this, a ticket whose text has not changed is skipped rather than re-embedded.
          </span>
        </Card.Body>
      </Card>

      {embed.data ? (
        <Alert variant={embed.data.failed ? 'warning' : 'success'} className="mb-3">
          Embedded <strong>{embed.data.embedded}</strong> of {embed.data.requested} in{' '}
          {embed.data.duration_seconds}s
          {embed.data.skipped ? <> · {embed.data.skipped} skipped (already current)</> : null}
          {embed.data.failed ? <> · {embed.data.failed} failed</> : null}
          {embed.data.errors.length ? (
            <ul className="mb-0 mt-2 small">
              {embed.data.errors.map((err) => (
                <li key={err}>{err}</li>
              ))}
            </ul>
          ) : null}
        </Alert>
      ) : null}

      {embed.isError ? (
        <Alert variant="danger" className="mb-3">
          {embed.error instanceof Error ? embed.error.message : 'Embedding failed.'}
        </Alert>
      ) : null}

      {remove.data ? (
        <Alert variant="success" className="mb-3">
          Deleted <strong>{remove.data.deleted}</strong> embedding
          {remove.data.deleted === 1 ? '' : 's'}. The tickets themselves are unchanged.
        </Alert>
      ) : null}

      {list.isLoading ? (
        <div className="text-center py-5">
          <Spinner animation="border" />
        </div>
      ) : list.isError ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : rows.length === 0 ? (
        <EmptyState title="No tickets match" description="Try clearing the filters." />
      ) : (
        <Card>
          <Table hover responsive className="mb-0 align-middle">
            <thead>
              <tr>
                <th style={{ width: 40 }}>
                  <Form.Check
                    type="checkbox"
                    aria-label="Select all visible"
                    checked={selected.size === rows.length && rows.length > 0}
                    onChange={toggleAll}
                  />
                </th>
                <th style={{ width: 90 }}>Ticket</th>
                <th>Title</th>
                <th style={{ width: 130 }}>Index state</th>
                <th style={{ width: 110 }}>Status</th>
                <th style={{ width: 90 }}>Chars</th>
                <th style={{ width: 170 }}>Embedded at</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <RowLine
                  key={row.ticket_id}
                  row={row}
                  checked={selected.has(row.ticket_id)}
                  onToggle={() => toggle(row.ticket_id)}
                />
              ))}
            </tbody>
          </Table>
        </Card>
      )}

      {list.data && list.data.total > rows.length ? (
        <p className="text-muted small mt-2">
          Showing {rows.length} of {list.data.total}. Narrow the filters to see the rest.
        </p>
      ) : null}
    </div>
  )
}

function Stat({
  label,
  value,
  bg,
  hint,
}: {
  label: string
  value: number | string
  bg?: string
  hint?: string
}) {
  return (
    <Col>
      <Card className="h-100" title={hint}>
        <Card.Body className="py-2">
          <div className="text-muted small">
            {label}
            {hint ? <span className="ms-1" aria-hidden="true">ⓘ</span> : null}
          </div>
          <div className="fs-4">
            {bg ? <Badge bg={bg}>{value}</Badge> : <strong>{value}</strong>}
          </div>
        </Card.Body>
      </Card>
    </Col>
  )
}

function RowLine({
  row,
  checked,
  onToggle,
}: {
  row: EmbeddingRow
  checked: boolean
  onToggle: () => void
}) {
  const meta = STATE_META[row.state]
  return (
    <tr>
      <td>
        <Form.Check
          type="checkbox"
          aria-label={`Select OS-${row.ticket_number}`}
          checked={checked}
          onChange={onToggle}
        />
      </td>
      <td>
        <a href={`/tickets/OS-${row.ticket_number}`}>OS-{row.ticket_number}</a>
      </td>
      <td className="text-truncate" style={{ maxWidth: 460 }} title={row.title}>
        {row.title}
      </td>
      <td>
        <Badge bg={meta.bg} title={meta.hint}>
          {meta.label}
        </Badge>
      </td>
      <td className="small text-muted">{row.status.replace(/_/g, ' ')}</td>
      <td className="small text-muted">{row.text_length}</td>
      <td className="small text-muted">
        {row.embedded_at ? new Date(row.embedded_at).toLocaleString() : '—'}
      </td>
    </tr>
  )
}
