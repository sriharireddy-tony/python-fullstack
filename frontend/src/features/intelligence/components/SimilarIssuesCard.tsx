import { useState } from 'react'
import { Badge, Button, Card, OverlayTrigger, Spinner, Tooltip } from 'react-bootstrap'
import { RelativeTime, StatusBadge, statusLabel } from '@/components/badges'
import { TicketPreviewModal } from '@/features/tickets/TicketPreviewModal'
import type { Relation, SimilarTicket, TicketStatus } from '@/api/types'
import { useDecideSuggestion, useSimilarTickets } from '../api/useSimilar'

/**
 * The Similar Issues panel.
 *
 * Four states that all matter, and are all different:
 *
 * - **results** — what we found, why we found it, and what the model thinks it
 *   means.
 * - **nothing similar** — a real answer, shown as one calm line. Most tickets
 *   genuinely have no near-duplicate, and padding the list with weak matches is
 *   how a panel like this earns a reputation for noise.
 * - **unavailable** — the search itself could not run. Deliberately *not*
 *   collapsed into "nothing similar": telling someone there are no duplicates
 *   when we never looked is worse than telling them we could not look.
 * - **refused** — an input guardrail declined. Also distinct, for the same
 *   reason.
 */
export function SimilarIssuesCard({ reference }: { reference: string }) {
  const { data, isLoading, error } = useSimilarTickets(reference)
  const decide = useDecideSuggestion(reference)
  const [showTrace, setShowTrace] = useState(false)
  // Which suggested ticket is open in the preview modal, if any. Held here
  // rather than per row so only one can ever be open -- nested previews are
  // how a comparison aid turns into a maze.
  const [preview, setPreview] = useState<string | null>(null)

  return (
    <Card className="mt-3">
      <Card.Header className="d-flex align-items-center justify-content-between">
        <span className="fw-semibold small">Similar issues</span>
        <div className="d-flex gap-1">
          {data?.rerank_model && (
            <OverlayTrigger
              overlay={
                <Tooltip>
                  Classified by {data.rerank_model}. Retrieval found these; the model judged
                  what they mean.
                </Tooltip>
              }
            >
              <Badge bg="info" text="dark">
                judged
              </Badge>
            </OverlayTrigger>
          )}
          {data?.degraded && (
            <OverlayTrigger
              overlay={
                <Tooltip>
                  One of the two searches was unavailable, so these results came from the other
                  alone.
                </Tooltip>
              }
            >
              <Badge bg="warning" text="dark">
                partial
              </Badge>
            </OverlayTrigger>
          )}
        </div>
      </Card.Header>

      <Card.Body className="small">
        {isLoading && (
          <div className="text-secondary">
            <Spinner animation="border" size="sm" className="me-2" />
            Searching earlier tickets…
          </div>
        )}

        {error && (
          <div className="text-secondary">
            Similar-issue search is unavailable right now. The rest of the ticket is unaffected.
          </div>
        )}

        {data?.blocked_reason && <div className="text-secondary">{data.blocked_reason}</div>}

        {data && !data.blocked_reason && data.results.length === 0 && (
          <div className="text-secondary">
            No earlier ticket looks like this one. That is usually the answer — most bugs are not
            duplicates.
          </div>
        )}

        {data && data.results.length > 0 && (
          <div className="d-flex flex-column gap-3">
            {data.results.map((item) => (
              <SimilarRow
                key={item.ticket_id}
                item={item}
                onOpen={() => setPreview(item.reference)}
                onDecide={(accepted) => {
                  if (item.suggestion_id) {
                    void decide.mutateAsync({
                      suggestionId: item.suggestion_id,
                      accepted,
                    })
                  }
                }}
                busy={decide.isPending}
              />
            ))}
          </div>
        )}

        {decide.isError && (
          <div className="alert alert-danger py-2 px-2 mt-2 mb-0 small">
            {(decide.error as Error).message}
          </div>
        )}

        {data && data.trace.length > 0 && (
          <>
            <button
              type="button"
              className="btn btn-link btn-sm p-0 mt-2 text-secondary"
              style={{ fontSize: '0.75rem' }}
              onClick={() => setShowTrace((open) => !open)}
            >
              {showTrace ? 'Hide' : 'How was this found?'}
            </button>
            {showTrace && (
              <div
                className="mt-2 p-2 bg-body-secondary rounded font-monospace text-secondary"
                style={{ fontSize: '0.7rem' }}
              >
                {data.trace.map((step, index) => (
                  <div key={`${step}-${index}`}>{step}</div>
                ))}
                <div className="mt-1">
                  {data.total_candidates} candidate{data.total_candidates === 1 ? '' : 's'} after
                  fusion
                  {data.rewrites > 0 && ` · ${data.rewrites} query rewrite(s)`}
                  {data.from_cache && ' · served from cache'}
                </div>
              </div>
            )}
          </>
        )}
      </Card.Body>

      <TicketPreviewModal reference={preview} onHide={() => setPreview(null)} />
    </Card>
  )
}

function SimilarRow({
  item,
  onOpen,
  onDecide,
  busy,
}: {
  item: SimilarTicket
  onOpen: () => void
  onDecide: (accepted: boolean) => void
  busy: boolean
}) {
  return (
    <div>
      <div className="d-flex align-items-center gap-2 flex-wrap">
        {/* A button, not a link: this opens a preview beside the ticket being
            read rather than navigating away from it. Comparing two tickets is
            the entire job of this panel, and a navigation costs you one of
            them. */}
        <button
          type="button"
          className="btn btn-link p-0 font-monospace align-baseline"
          style={{ fontSize: 'inherit' }}
          onClick={onOpen}
        >
          {item.reference}
        </button>
        <StatusBadge status={item.status as TicketStatus} />
        <MatchScore similarity={item.similarity} />
        {item.relation ? (
          <RelationBadge relation={item.relation} confidence={item.confidence} />
        ) : (
          <SourceBadges sources={item.sources} agreed={item.agreed} />
        )}
      </div>

      <button
        type="button"
        className="btn btn-link p-0 fw-medium text-start text-body d-block"
        style={{ fontSize: 'inherit', textDecoration: 'none' }}
        onClick={onOpen}
      >
        {item.title}
      </button>

      {/* The model's justification, when a model ran. Shown because a
          classification without a reason is an assertion, and an assertion is
          not something a reader can check. */}
      {item.reason && (
        <div className="text-secondary fst-italic mt-1" style={{ fontSize: '0.8rem' }}>
          {item.reason}
        </div>
      )}

      {/* The most useful field on a similar ticket: how it was fixed. Only
          present once the ticket is closed, which is exactly when it is
          trustworthy. */}
      {item.resolution_summary && (
        <div className="text-secondary mt-1" style={{ fontSize: '0.8rem' }}>
          <span className="fw-medium">Resolved:</span> {item.resolution_summary}
        </div>
      )}

      <div className="d-flex align-items-center justify-content-between gap-2 mt-1">
        <div className="text-secondary" style={{ fontSize: '0.75rem' }}>
          {item.closed_at ? (
            <>
              closed <RelativeTime value={item.closed_at} />
            </>
          ) : (
            <>
              {statusLabel(item.status as TicketStatus).toLowerCase()} · raised{' '}
              <RelativeTime value={item.created_at} />
            </>
          )}
        </div>

        {/* Present only for a judged result. Every click is a labelled pair
            for the evaluation set, which is why the buttons exist at all
            rather than the panel being read-only. */}
        {item.suggestion_id && (
          <div className="d-flex gap-1">
            <Button
              variant="outline-success"
              size="sm"
              className="py-0 px-2"
              style={{ fontSize: '0.7rem' }}
              disabled={busy}
              onClick={() => onDecide(true)}
            >
              Agree
            </Button>
            <Button
              variant="outline-secondary"
              size="sm"
              className="py-0 px-2"
              style={{ fontSize: '0.7rem' }}
              disabled={busy}
              onClick={() => onDecide(false)}
            >
              Not this
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * How alike the two tickets are, as the vector store measured it.
 *
 * ## Why this is `similarity` and not `score`
 *
 * The response also carries a fused `score`, and it would be the obvious thing
 * to show. It is the wrong thing: fusion ranks by *position*, not by distance,
 * so every result lands near 0.016 whatever its actual likeness. Two tickets
 * about the same bug and two about nothing in common produce nearly the same
 * fused score. Shown to a human that is worse than showing nothing, because it
 * looks like a measurement.
 *
 * `similarity` is the raw cosine from the vector store: 0 to 1, larger is
 * closer, and it means what a reader assumes it means.
 *
 * ## Why the bands are cautious
 *
 * Cosine similarity between two pieces of ordinary English rarely goes below
 * about 0.3 even when the texts are unrelated, so a naive percentage reads far
 * too generously — 40% sounds like a weak match when it is really no match at
 * all. The bands below are set against that floor, and the label carries the
 * meaning while the number stays visible for anyone calibrating their own
 * judgement.
 *
 * Absent for a keyword-only hit: nothing was compared in vector space, and
 * showing 0% would claim the opposite of "not measured".
 */
function MatchScore({ similarity }: { similarity: number | null }) {
  if (similarity === null) return null

  const percent = Math.round(similarity * 100)
  const { label, bg } =
    similarity >= 0.8
      ? { label: 'very close', bg: 'success' }
      : similarity >= 0.65
        ? { label: 'close', bg: 'primary' }
        : similarity >= 0.5
          ? { label: 'loose', bg: 'secondary' }
          : { label: 'distant', bg: 'light' }

  return (
    <OverlayTrigger
      overlay={
        <Tooltip>
          Cosine similarity {similarity.toFixed(3)} between the two tickets' embeddings. Above
          about 0.8 the two usually describe the same problem; below 0.5 they rarely do.
        </Tooltip>
      }
    >
      <Badge bg={bg} text={bg === 'light' ? 'dark' : undefined}>
        {percent}% · {label}
      </Badge>
    </OverlayTrigger>
  )
}

const RELATION_LABELS: Record<Relation, { label: string; bg: string; help: string }> = {
  duplicate: {
    label: 'duplicate',
    bg: 'danger',
    help: 'The same defect, reported again.',
  },
  recurring: {
    label: 'recurring',
    bg: 'warning',
    help: 'The same defect as one already closed — likely a regression.',
  },
  related: {
    label: 'related',
    bg: 'secondary',
    help: 'A different defect in the same area. Context, not the same bug.',
  },
  unrelated: {
    label: 'unrelated',
    bg: 'light',
    help: 'Not useful here. These are filtered out before display.',
  },
}

/**
 * The model's verdict, with its confidence.
 *
 * Confidence is shown as a number rather than hidden behind a colour, because
 * the panel is going to be wrong sometimes and a reader who can see "0.62"
 * calibrates differently from one shown a flat "duplicate".
 */
function RelationBadge({
  relation,
  confidence,
}: {
  relation: Relation
  confidence: number | null
}) {
  const meta = RELATION_LABELS[relation]
  return (
    <OverlayTrigger overlay={<Tooltip>{meta.help}</Tooltip>}>
      <Badge bg={meta.bg} text={meta.bg === 'light' || meta.bg === 'warning' ? 'dark' : undefined}>
        {meta.label}
        {confidence !== null && ` ${confidence.toFixed(2)}`}
      </Badge>
    </OverlayTrigger>
  )
}

/**
 * Why this result is here, when no model judged it.
 *
 * Worth the pixels because this panel will sometimes be wrong, and a reader who
 * can see *how* a result was found calibrates far faster than one shown a bare
 * list. "Exact match" and "both searches agree" mean very different things from
 * "keyword only".
 */
function SourceBadges({ sources, agreed }: { sources: string[]; agreed: boolean }) {
  if (sources.includes('reference')) {
    return (
      <OverlayTrigger
        overlay={<Tooltip>Named directly in this ticket, or an exact identifier match.</Tooltip>}
      >
        <Badge bg="dark">exact match</Badge>
      </OverlayTrigger>
    )
  }

  if (agreed) {
    return (
      <OverlayTrigger
        overlay={
          <Tooltip>
            Both the meaning-based and the keyword search ranked this highly — the strongest signal
            available without a language model.
          </Tooltip>
        }
      >
        <Badge bg="success">both searches</Badge>
      </OverlayTrigger>
    )
  }

  const label = sources.includes('semantic') ? 'similar wording' : 'keyword match'
  return (
    <OverlayTrigger
      overlay={
        <Tooltip>
          {sources.includes('semantic')
            ? 'Found by meaning, so the wording may differ completely.'
            : 'Found by shared terms — often an error code or product name.'}
        </Tooltip>
      }
    >
      <Badge bg="secondary">{label}</Badge>
    </OverlayTrigger>
  )
}
