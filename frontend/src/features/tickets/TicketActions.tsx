/**
 * Contextual ticket actions.
 *
 * Buttons render from `available_actions`, which the server computes from the
 * user's permissions *and* the ticket's current state. A developer looking at a
 * resolved ticket sees nothing actionable; CS sees Close and Reopen.
 *
 * Every action carries the version the client read. A mismatch returns 409 and
 * the user is told to reload rather than silently overwriting a colleague.
 */

import { useState } from 'react'
import { Alert, Button, Form, Modal, Spinner } from 'react-bootstrap'
import { ApiError } from '@/api/client'
import { useTicketAction } from '@/api/hooks/useTickets'
import { useMetaEnums } from '@/api/hooks/useTickets'
import { useTeams, useUsers } from '@/api/hooks/useAdmin'
import type { TicketDetail } from '@/api/types'

interface ActionSpec {
  label: string
  variant: string
  /** Fields the action requires beyond the version. */
  fields: 'none' | 'assign' | 'hold' | 'resolve' | 'reject' | 'close' | 'reopen' | 'transfer' | 'priority'
  confirm?: string
}

const ACTIONS: Record<string, ActionSpec> = {
  assign: { label: 'Assign', variant: 'primary', fields: 'assign' },
  start: { label: 'Start work', variant: 'primary', fields: 'none' },
  hold: { label: 'Put on hold', variant: 'outline-secondary', fields: 'hold' },
  resolve: { label: 'Mark resolved', variant: 'success', fields: 'resolve' },
  reject: { label: 'Reject', variant: 'outline-dark', fields: 'reject' },
  close: {
    label: 'Close ticket',
    variant: 'success',
    fields: 'close',
    confirm: 'Only close once the client has confirmed the issue is resolved.',
  },
  reopen: { label: 'Reopen', variant: 'warning', fields: 'reopen' },
  'transfer-team': { label: 'Transfer team', variant: 'outline-secondary', fields: 'transfer' },
  'override-priority': {
    label: 'Override priority',
    variant: 'outline-warning',
    fields: 'priority',
  },
}

export function TicketActions({ ticket }: { ticket: TicketDetail }) {
  const [active, setActive] = useState<string | null>(null)

  const available = ticket.available_actions.filter((a) => a in ACTIONS)
  if (available.length === 0) {
    return (
      <div className="text-secondary small fst-italic">
        No actions available to you on this ticket.
      </div>
    )
  }

  return (
    <>
      <div className="d-flex flex-wrap gap-2">
        {available.map((action) => (
          <Button
            key={action}
            size="sm"
            variant={ACTIONS[action]!.variant}
            onClick={() => setActive(action)}
          >
            {ACTIONS[action]!.label}
          </Button>
        ))}
      </div>

      {active && (
        <ActionModal
          action={active}
          spec={ACTIONS[active]!}
          ticket={ticket}
          onClose={() => setActive(null)}
        />
      )}
    </>
  )
}

function ActionModal({
  action,
  spec,
  ticket,
  onClose,
}: {
  action: string
  spec: ActionSpec
  ticket: TicketDetail
  onClose: () => void
}) {
  const mutation = useTicketAction(ticket.reference)
  const { data: enums } = useMetaEnums()
  const { data: teams } = useTeams(true)
  const { data: users } = useUsers({ activeOnly: true })
  const [values, setValues] = useState<Record<string, string>>({})

  function set(key: string, value: string) {
    setValues((current) => ({ ...current, [key]: value }))
  }

  const requirements: Record<string, string[]> = {
    resolve: ['resolution_notes'],
    reject: ['rejection_reason', 'note'],
    reopen: ['reason'],
    transfer: ['team_id', 'reason'],
    priority: ['priority', 'reason'],
    hold: ['waiting_on'],
  }
  const missing = (requirements[spec.fields] ?? []).filter((f) => !values[f]?.trim())

  async function submit() {
    const body: Record<string, unknown> = { version: ticket.version, ...values }
    if (spec.fields === 'assign') {
      body.assignee_id = values.assignee_id || null
    }
    await mutation.mutateAsync({ action, body })
    onClose()
  }

  return (
    <Modal show onHide={onClose} centered>
      <Modal.Header closeButton>
        <Modal.Title className="h6">
          {spec.label} · {ticket.reference}
        </Modal.Title>
      </Modal.Header>

      <Modal.Body>
        {spec.confirm && (
          <Alert variant="warning" className="small py-2">
            {spec.confirm}
          </Alert>
        )}

        {mutation.isError && (
          <Alert variant="danger" className="small py-2">
            {mutation.error instanceof ApiError && mutation.error.isVersionConflict
              ? 'This ticket was changed by someone else. Reload the page to see the latest version.'
              : mutation.error instanceof ApiError
                ? mutation.error.message
                : 'The action failed.'}
          </Alert>
        )}

        {spec.fields === 'assign' && (
          <Form.Group>
            <Form.Label className="small fw-medium">Assign to</Form.Label>
            <Form.Select
              value={values.assignee_id ?? ''}
              onChange={(e) => set('assignee_id', e.target.value)}
            >
              <option value="">Unassigned (return to the team inbox)</option>
              {users?.items.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.full_name}
                </option>
              ))}
            </Form.Select>
            <Form.Text className="text-secondary">
              Developers may only assign tickets to themselves.
            </Form.Text>
          </Form.Group>
        )}

        {spec.fields === 'hold' && (
          <>
            <Form.Group className="mb-3">
              <Form.Label className="small fw-medium">Waiting on</Form.Label>
              <Form.Select
                value={values.waiting_on ?? ''}
                onChange={(e) => set('waiting_on', e.target.value)}
              >
                <option value="">Select…</option>
                {enums?.waiting_on.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </Form.Select>
            </Form.Group>
            <TextArea label="Note (optional)" onChange={(v) => set('note', v)} />
          </>
        )}

        {spec.fields === 'resolve' && (
          <TextArea
            label="What was done?"
            required
            hint="CS will read this before confirming with the client."
            onChange={(v) => set('resolution_notes', v)}
          />
        )}

        {spec.fields === 'reject' && (
          <>
            <Form.Group className="mb-3">
              <Form.Label className="small fw-medium">Reason</Form.Label>
              <Form.Select
                value={values.rejection_reason ?? ''}
                onChange={(e) => set('rejection_reason', e.target.value)}
              >
                <option value="">Select…</option>
                {enums?.rejection_reasons.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </Form.Select>
            </Form.Group>
            <TextArea
              label="Explain the rejection"
              required
              hint="This goes back to CS, who will explain it to the client."
              onChange={(v) => set('note', v)}
            />
          </>
        )}

        {spec.fields === 'close' && (
          <TextArea label="Closing note (optional)" onChange={(v) => set('note', v)} />
        )}

        {spec.fields === 'reopen' && (
          <TextArea
            label="Why is this being reopened?"
            required
            onChange={(v) => set('reason', v)}
          />
        )}

        {spec.fields === 'transfer' && (
          <>
            <Form.Group className="mb-3">
              <Form.Label className="small fw-medium">Move to team</Form.Label>
              <Form.Select
                value={values.team_id ?? ''}
                onChange={(e) => set('team_id', e.target.value)}
              >
                <option value="">Select a team…</option>
                {teams
                  ?.filter((t) => t.id !== ticket.team?.id)
                  .map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
              </Form.Select>
            </Form.Group>
            <TextArea
              label="Why?"
              required
              hint="Misrouting is normal. The reason helps tune the team list."
              onChange={(v) => set('reason', v)}
            />
          </>
        )}

        {spec.fields === 'priority' && (
          <>
            <Form.Group className="mb-3">
              <Form.Label className="small fw-medium">New priority</Form.Label>
              <Form.Select
                value={values.priority ?? ''}
                onChange={(e) => set('priority', e.target.value)}
              >
                <option value="">Select…</option>
                {enums?.priorities.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </Form.Select>
            </Form.Group>
            <TextArea
              label="Justification"
              required
              hint="Recorded. Frequent overrides mean the severity matrix needs tuning."
              onChange={(v) => set('reason', v)}
            />
          </>
        )}
      </Modal.Body>

      <Modal.Footer>
        <Button variant="outline-secondary" size="sm" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant={spec.variant.replace('outline-', '')}
          size="sm"
          disabled={missing.length > 0 || mutation.isPending}
          onClick={submit}
        >
          {mutation.isPending ? (
            <>
              <Spinner as="span" animation="border" size="sm" className="me-2" />
              Working…
            </>
          ) : (
            spec.label
          )}
        </Button>
      </Modal.Footer>
    </Modal>
  )
}

function TextArea({
  label,
  hint,
  required,
  onChange,
}: {
  label: string
  hint?: string
  required?: boolean
  onChange: (value: string) => void
}) {
  return (
    <Form.Group>
      <Form.Label className="small fw-medium">
        {label}
        {required && <span className="text-danger ms-1">*</span>}
      </Form.Label>
      <Form.Control as="textarea" rows={3} onChange={(e) => onChange(e.target.value)} />
      {hint && <Form.Text className="text-secondary">{hint}</Form.Text>}
    </Form.Group>
  )
}
