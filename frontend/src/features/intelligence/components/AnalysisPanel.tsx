import { Alert, Badge, Button, ListGroup, ProgressBar, Spinner } from 'react-bootstrap'
import { Link } from 'react-router-dom'
import { EmptyState } from '@/components/states'
import type { AnalysisRun } from '@/api/types'
import { useAnalysis, useRateAnalysis, useRequestAnalysis } from '../api/useAnalysis'

const RUNNING = new Set(['queued', 'running'])

/** Tool name to something a person reading a progress list would recognise. */
const STEP_LABELS: Record<string, string> = {
  get_ticket: 'Read the ticket',
  find_similar_tickets: 'Searched for earlier reports of the same problem',
  search_tickets: 'Searched tickets',
  get_comments: 'Read the conversation',
  get_history: 'Checked the status history',
  count_tickets: 'Counted tickets',
  reopen_rate: 'Checked reopen rates',
}

/**
 * The Analysis tab.
 *
 * The design decision worth naming: the **step log is shown while the run is
 * going**, not just the finished report. A run takes tens of seconds, and the
 * difference between a tolerable wait and an intolerable one is whether the
 * user can see what is happening. "Read the ticket → searched for earlier
 * reports → checked the status history" is a progress bar that also happens to
 * be the audit trail.
 *
 * The second decision: a partial report is shown, clearly labelled. An
 * analysis that stopped at a limit having found two related tickets is worth
 * reading; hiding it because it is incomplete throws away work the user paid
 * for.
 */
export function AnalysisPanel({ reference }: { reference: string }) {
  const { data: run, isLoading } = useAnalysis(reference)
  const request = useRequestAnalysis(reference)
  const rate = useRateAnalysis(reference)

  if (isLoading) {
    return (
      <div className="text-secondary small">
        <Spinner animation="border" size="sm" className="me-2" />
        Loading…
      </div>
    )
  }

  if (!run) {
    return (
      <div>
        <EmptyState
          title="No analysis yet"
          description={
            'An AI agent can read this ticket, search for earlier reports of the same ' +
            'problem, and suggest where to look. It takes under a minute.'
          }
        />
        <div className="d-flex justify-content-center">
          <Button
            size="sm"
            onClick={() => void request.mutateAsync()}
            disabled={request.isPending}
          >
            {request.isPending ? 'Starting…' : 'Analyse this ticket'}
          </Button>
        </div>
        {request.isError && (
          <Alert variant="danger" className="py-2 small mt-3 mb-0">
            {(request.error as Error).message}
          </Alert>
        )}
      </div>
    )
  }

  const running = RUNNING.has(run.status)

  return (
    <div className="small">
      <div className="d-flex align-items-center justify-content-between gap-2 mb-3">
        <div className="d-flex align-items-center gap-2">
          <StatusBadge run={run} />
          {run.used_fallback_model && (
            // Surfaced rather than hidden: a reader calibrating on "the AI
            // said" deserves to know it was the weaker local model.
            <Badge bg="secondary" title={`Produced by ${run.model}`}>
              local model
            </Badge>
          )}
        </div>
        {!running && (
          <Button
            variant="outline-secondary"
            size="sm"
            className="py-0 px-2"
            style={{ fontSize: '0.75rem' }}
            onClick={() => void request.mutateAsync()}
            disabled={request.isPending}
          >
            Run again
          </Button>
        )}
      </div>

      {running && (
        <>
          <ProgressBar animated now={100} style={{ height: 4 }} className="mb-3" />
          <div className="text-secondary mb-3">
            Working… this usually takes under a minute. You can leave this page; the run
            continues.
          </div>
        </>
      )}

      {run.steps.length > 0 && (
        <ListGroup variant="flush" className="mb-3">
          {run.steps.map((step) => (
            <ListGroup.Item key={`${step.turn}-${step.created_at}`} className="px-0 py-1 border-0">
              <span className="text-success me-2">✓</span>
              <span>{STEP_LABELS[step.tool_name ?? ''] ?? step.tool_name ?? 'Thinking'}</span>
              {step.result_summary && (
                <span className="text-secondary ms-2" style={{ fontSize: '0.75rem' }}>
                  {step.result_summary}
                </span>
              )}
            </ListGroup.Item>
          ))}
        </ListGroup>
      )}

      {run.partial && run.stop_reason && (
        <Alert variant="warning" className="py-2 small">
          <strong>Stopped early.</strong> This analysis {run.stop_reason}. What it found is
          below — treat it as incomplete.
        </Alert>
      )}

      {run.status === 'failed' && (
        <Alert variant="danger" className="py-2 small">
          The analysis could not complete. {run.stop_reason}
        </Alert>
      )}

      {run.report && <Report run={run} />}

      {run.report && (
        <div className="d-flex align-items-center gap-2 mt-3 pt-3 border-top">
          <span className="text-secondary">Was this useful?</span>
          <Button
            variant={run.helpful === true ? 'success' : 'outline-success'}
            size="sm"
            className="py-0 px-2"
            style={{ fontSize: '0.75rem' }}
            disabled={rate.isPending}
            onClick={() => void rate.mutateAsync({ runId: run.id, helpful: true })}
          >
            Yes
          </Button>
          <Button
            variant={run.helpful === false ? 'secondary' : 'outline-secondary'}
            size="sm"
            className="py-0 px-2"
            style={{ fontSize: '0.75rem' }}
            disabled={rate.isPending}
            onClick={() => void rate.mutateAsync({ runId: run.id, helpful: false })}
          >
            No
          </Button>
          <span className="text-secondary ms-auto" style={{ fontSize: '0.7rem' }}>
            {run.turns} turn{run.turns === 1 ? '' : 's'} · {run.tool_calls} lookups
            {run.latency_ms !== null && ` · ${(run.latency_ms / 1000).toFixed(1)}s`}
          </span>
        </div>
      )}
    </div>
  )
}

function Report({ run }: { run: AnalysisRun }) {
  const report = run.report
  if (!report) return null

  return (
    <div>
      <div className="d-flex align-items-center gap-2 mb-2">
        <Badge bg="light" text="dark">
          {report.likely_area}
        </Badge>
        {report.is_recurring && (
          <Badge bg="warning" text="dark" title="This problem was closed before and has come back">
            recurring
          </Badge>
        )}
        {/* Confidence as a number, not a colour. This will be wrong
            sometimes, and a reader who sees 0.55 weighs it differently from
            one shown a confident-looking panel. */}
        <span className="text-secondary" style={{ fontSize: '0.75rem' }}>
          confidence {report.confidence.toFixed(2)}
        </span>
      </div>

      <p className="mb-3">{report.summary}</p>

      {report.suggested_next_steps.length > 0 && (
        <>
          <div className="fw-medium mb-1">Suggested next steps</div>
          <ul className="ps-3 mb-3">
            {report.suggested_next_steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ul>
        </>
      )}

      {report.related_references.length > 0 && (
        <>
          <div className="fw-medium mb-1">Earlier tickets it looked at</div>
          <div className="d-flex flex-wrap gap-2">
            {report.related_references.map((ref) => (
              <Link key={ref} to={`/tickets/${ref}`} className="font-monospace">
                {ref}
              </Link>
            ))}
          </div>
          {/* Worth stating plainly: these were validated against what the
              agent actually retrieved, so they are not invented. */}
          <div className="text-secondary mt-1" style={{ fontSize: '0.7rem' }}>
            Every reference above was checked against the tickets the agent actually read.
          </div>
        </>
      )}
    </div>
  )
}

function StatusBadge({ run }: { run: AnalysisRun }) {
  const map: Record<string, { label: string; bg: string }> = {
    queued: { label: 'queued', bg: 'secondary' },
    running: { label: 'running', bg: 'primary' },
    completed: { label: run.partial ? 'partial' : 'complete', bg: run.partial ? 'warning' : 'success' },
    failed: { label: 'failed', bg: 'danger' },
    budget_exceeded: { label: 'stopped at a limit', bg: 'warning' },
    timed_out: { label: 'timed out', bg: 'warning' },
  }
  const meta = map[run.status] ?? { label: run.status, bg: 'secondary' }
  return (
    <Badge bg={meta.bg} text={meta.bg === 'warning' || meta.bg === 'light' ? 'dark' : undefined}>
      {meta.label}
    </Badge>
  )
}
