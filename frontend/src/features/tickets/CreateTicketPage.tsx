/**
 * Raise a ticket.
 *
 * The form deliberately has **no priority field**. CS answers severity, impact,
 * and workaround — observable facts — and the derived priority is shown live as
 * a preview. That is the mechanism that stops everything becoming P1.
 *
 * The preview calls the same pure function the server uses, so what is
 * previewed can never disagree with what gets stored.
 */

import { useEffect, useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Alert, Button, Card, Col, Form, Row, Spinner } from 'react-bootstrap'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { useClients, useTeams } from '@/api/hooks/useAdmin'
import { useCreateTicket, useMetaEnums, usePriorityPreview } from '@/api/hooks/useTickets'
import { PriorityBadge } from '@/components/badges'
import { PageHeader } from '@/components/PageHeader'
import type { Priority } from '@/api/types'

const schema = z.object({
  title: z.string().min(3, 'Give the ticket a short, specific title').max(300),
  description: z.string().min(1, 'Describe the problem'),
  client_id: z.string().min(1, 'Select the client who reported this'),
  team_id: z.string().min(1, 'Select the team that owns this area'),
  environment: z.string().min(1, 'Select the environment'),
  severity: z.string().min(1, 'How broken is it?'),
  impact: z.string().min(1, 'Who is affected?'),
  workaround: z.string().min(1, 'Is there a workaround?'),
  steps_to_reproduce: z.string().optional(),
  expected_result: z.string().optional(),
  actual_result: z.string().optional(),
  reporter_name: z.string().max(200).optional(),
  reporter_email: z.string().email('Enter a valid email').or(z.literal('')).optional(),
})

type FormValues = z.infer<typeof schema>

export function CreateTicketPage() {
  const navigate = useNavigate()
  const { data: enums } = useMetaEnums()
  const { data: teams } = useTeams(true)
  const { data: clients } = useClients({ activeOnly: true })
  const createTicket = useCreateTicket()
  const preview = usePriorityPreview()
  const [derived, setDerived] = useState<{ priority: Priority; explanation: string } | null>(null)

  const {
    register,
    handleSubmit,
    watch,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { environment: 'production' },
  })

  const severity = watch('severity')
  const impact = watch('impact')
  const workaround = watch('workaround')

  // Recompute the preview whenever all three inputs are answered.
  useEffect(() => {
    if (!severity || !impact || !workaround) {
      setDerived(null)
      return
    }
    preview.mutate(
      { severity, impact, workaround },
      { onSuccess: setDerived },
    )
    // preview.mutate is stable; including it would loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [severity, impact, workaround])

  async function onSubmit(values: FormValues) {
    const payload = {
      ...values,
      reporter_email: values.reporter_email || undefined,
      steps_to_reproduce: values.steps_to_reproduce || undefined,
      expected_result: values.expected_result || undefined,
      actual_result: values.actual_result || undefined,
    }
    const ticket = await createTicket.mutateAsync(payload)
    navigate(`/tickets/${ticket.reference}`)
  }

  return (
    <>
      <PageHeader
        title="Raise a ticket"
        subtitle="Describe what the client reported. Priority is calculated from your answers."
      />

      {createTicket.isError && (
        <Alert variant="danger" className="small">
          {createTicket.error instanceof ApiError
            ? createTicket.error.message
            : 'Could not create the ticket.'}
        </Alert>
      )}

      <Form onSubmit={handleSubmit(onSubmit)} noValidate>
        <Row className="g-3">
          <Col lg={8}>
            <Card className="mb-3">
              <Card.Header className="fw-semibold small">The problem</Card.Header>
              <Card.Body>
                <Field label="Title" error={errors.title?.message} required>
                  <Form.Control
                    isInvalid={Boolean(errors.title)}
                    placeholder="Payroll export fails for employees with mid-month joins"
                    {...register('title')}
                  />
                </Field>

                <Field label="Description" error={errors.description?.message} required>
                  <Form.Control
                    as="textarea"
                    rows={4}
                    isInvalid={Boolean(errors.description)}
                    placeholder="What did the client report?"
                    {...register('description')}
                  />
                </Field>

                <Field
                  label="Steps to reproduce"
                  hint="Each round trip this prevents saves the developer an hour."
                >
                  <Form.Control as="textarea" rows={3} {...register('steps_to_reproduce')} />
                </Field>

                <Row>
                  <Col md={6}>
                    <Field label="Expected result">
                      <Form.Control as="textarea" rows={2} {...register('expected_result')} />
                    </Field>
                  </Col>
                  <Col md={6}>
                    <Field label="Actual result">
                      <Form.Control as="textarea" rows={2} {...register('actual_result')} />
                    </Field>
                  </Col>
                </Row>
              </Card.Body>
            </Card>

            <Card>
              <Card.Header className="fw-semibold small">Who reported it</Card.Header>
              <Card.Body>
                <Row>
                  <Col md={6}>
                    <Field label="Client" error={errors.client_id?.message} required>
                      <Form.Select isInvalid={Boolean(errors.client_id)} {...register('client_id')}>
                        <option value="">Select a client…</option>
                        {clients?.items.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.name}
                          </option>
                        ))}
                      </Form.Select>
                    </Field>
                  </Col>
                  <Col md={6}>
                    <Field label="Environment" error={errors.environment?.message} required>
                      <Form.Select {...register('environment')}>
                        {enums?.environments.map((o) => (
                          <option key={o.value} value={o.value}>
                            {o.label}
                          </option>
                        ))}
                      </Form.Select>
                    </Field>
                  </Col>
                </Row>
                <Row>
                  <Col md={6}>
                    <Field label="Contact name" hint="Who do we call back?">
                      <Form.Control {...register('reporter_name')} />
                    </Field>
                  </Col>
                  <Col md={6}>
                    <Field label="Contact email" error={errors.reporter_email?.message}>
                      <Form.Control
                        type="email"
                        isInvalid={Boolean(errors.reporter_email)}
                        {...register('reporter_email')}
                      />
                    </Field>
                  </Col>
                </Row>
              </Card.Body>
            </Card>
          </Col>

          <Col lg={4}>
            <Card className="mb-3">
              <Card.Header className="fw-semibold small">Urgency</Card.Header>
              <Card.Body>
                <Field label="How broken is it?" error={errors.severity?.message} required>
                  <Form.Select isInvalid={Boolean(errors.severity)} {...register('severity')}>
                    <option value="">Select…</option>
                    {enums?.severities.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Form.Select>
                </Field>

                <Field label="Who is affected?" error={errors.impact?.message} required>
                  <Form.Select isInvalid={Boolean(errors.impact)} {...register('impact')}>
                    <option value="">Select…</option>
                    {enums?.impacts.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Form.Select>
                </Field>

                <Field label="Is there a workaround?" error={errors.workaround?.message} required>
                  <Form.Select isInvalid={Boolean(errors.workaround)} {...register('workaround')}>
                    <option value="">Select…</option>
                    {enums?.workarounds.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Form.Select>
                </Field>

                <div className="border-top pt-3 mt-1">
                  <div className="small text-secondary mb-2">Calculated priority</div>
                  {derived ? (
                    <>
                      <div className="fs-4">
                        <PriorityBadge priority={derived.priority} />
                      </div>
                      <div className="small text-secondary mt-2">{derived.explanation}</div>
                    </>
                  ) : (
                    <div className="small text-secondary fst-italic">
                      Answer the three questions above.
                    </div>
                  )}
                </div>
              </Card.Body>
            </Card>

            <Card className="mb-3">
              <Card.Header className="fw-semibold small">Routing</Card.Header>
              <Card.Body>
                <Field
                  label="Owning team"
                  error={errors.team_id?.message}
                  hint="If you pick the wrong one, a manager can transfer it."
                  required
                >
                  <Form.Select isInvalid={Boolean(errors.team_id)} {...register('team_id')}>
                    <option value="">Select a team…</option>
                    {teams?.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.name}
                      </option>
                    ))}
                  </Form.Select>
                </Field>
                <p className="small text-secondary mb-0">
                  The ticket waits in that team&apos;s inbox until someone takes it.
                </p>
              </Card.Body>
            </Card>

            <div className="d-grid gap-2">
              <Button type="submit" variant="primary" disabled={isSubmitting}>
                {isSubmitting ? (
                  <>
                    <Spinner as="span" animation="border" size="sm" className="me-2" />
                    Creating…
                  </>
                ) : (
                  'Create ticket'
                )}
              </Button>
              <Button variant="outline-secondary" onClick={() => navigate(-1)}>
                Cancel
              </Button>
            </div>
          </Col>
        </Row>
      </Form>
    </>
  )
}

function Field({
  label,
  error,
  hint,
  required,
  children,
}: {
  label: string
  error?: string
  hint?: string
  required?: boolean
  children: React.ReactNode
}) {
  return (
    <Form.Group className="mb-3">
      <Form.Label className="small fw-medium">
        {label}
        {required && <span className="text-danger ms-1">*</span>}
      </Form.Label>
      {children}
      {error && <div className="invalid-feedback d-block">{error}</div>}
      {hint && !error && <Form.Text className="text-secondary">{hint}</Form.Text>}
    </Form.Group>
  )
}
