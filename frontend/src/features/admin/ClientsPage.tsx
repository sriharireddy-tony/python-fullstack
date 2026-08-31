import { useState } from 'react'
import { Badge, Button, Card, Form, Modal, Table } from 'react-bootstrap'
import { ApiError } from '@/api/client'
import { useClients, useDeleteClient, useSaveClient } from '@/api/hooks/useAdmin'
import { useMetaEnums } from '@/api/hooks/useTickets'
import { EmptyState, ErrorState, LoadingState } from '@/components/states'
import { PageHeader } from '@/components/PageHeader'
import type { Client } from '@/api/types'

const TIER_VARIANT: Record<string, string> = {
  standard: 'secondary',
  premium: 'info',
  enterprise: 'primary',
}

export function ClientsPage() {
  const { data, isLoading, error, refetch } = useClients()
  const [editing, setEditing] = useState<Partial<Client> | null>(null)
  const remove = useDeleteClient()

  return (
    <>
      <PageHeader
        title="Clients"
        subtitle="The customer companies whose issues are tracked. Selecting one is mandatory on every ticket."
        actions={
          <Button size="sm" onClick={() => setEditing({})}>
            + Add client
          </Button>
        }
      />

      {isLoading && <LoadingState />}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}

      {data && data.items.length === 0 && (
        <EmptyState
          title="No clients yet"
          description="Add the companies your support team receives issues from."
          action={
            <Button size="sm" onClick={() => setEditing({})}>
              Add the first client
            </Button>
          }
        />
      )}

      {data && data.items.length > 0 && (
        <Card>
          <Table hover responsive className="os-table mb-0 align-middle">
            <thead>
              <tr>
                <th>Name</th>
                <th style={{ width: 110 }}>Code</th>
                <th style={{ width: 120 }}>Tier</th>
                <th style={{ width: 100 }}>Status</th>
                <th style={{ width: 140 }} />
              </tr>
            </thead>
            <tbody>
              {data.items.map((client) => (
                <tr key={client.id}>
                  <td>{client.name}</td>
                  <td className="font-monospace small">{client.code}</td>
                  <td>
                    <Badge bg={TIER_VARIANT[client.tier] ?? 'secondary'}>{client.tier}</Badge>
                  </td>
                  <td>
                    {client.is_active ? (
                      <span className="text-success small">Active</span>
                    ) : (
                      <span className="text-secondary small">Inactive</span>
                    )}
                  </td>
                  <td className="text-end">
                    <Button variant="link" size="sm" onClick={() => setEditing(client)}>
                      Edit
                    </Button>
                    {client.is_active && (
                      <Button
                        variant="link"
                        size="sm"
                        className="text-danger"
                        onClick={() => void remove.mutateAsync(client.id)}
                      >
                        Deactivate
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}

      {editing && <ClientModal client={editing} onClose={() => setEditing(null)} />}
    </>
  )
}

function ClientModal({ client, onClose }: { client: Partial<Client>; onClose: () => void }) {
  const save = useSaveClient()
  const { data: enums } = useMetaEnums()
  const [form, setForm] = useState<Partial<Client>>({
    tier: 'standard',
    ...client,
  })

  const isNew = !client.id
  const valid = Boolean(form.name?.trim() && form.code?.trim())

  return (
    <Modal show onHide={onClose} centered>
      <Modal.Header closeButton>
        <Modal.Title className="h6">{isNew ? 'Add client' : 'Edit client'}</Modal.Title>
      </Modal.Header>
      <Modal.Body>
        {save.isError && (
          <div className="alert alert-danger py-2 small">
            {save.error instanceof ApiError ? save.error.message : 'Could not save.'}
          </div>
        )}

        <Form.Group className="mb-3">
          <Form.Label className="small fw-medium">Name</Form.Label>
          <Form.Control
            value={form.name ?? ''}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </Form.Group>

        <Form.Group className="mb-3">
          <Form.Label className="small fw-medium">Code</Form.Label>
          <Form.Control
            value={form.code ?? ''}
            onChange={(e) => setForm({ ...form, code: e.target.value.toUpperCase() })}
            disabled={!isNew}
          />
          <Form.Text className="text-secondary">
            Short identifier. Fixed once created, so historical tickets stay readable.
          </Form.Text>
        </Form.Group>

        <Form.Group className="mb-3">
          <Form.Label className="small fw-medium">Tier</Form.Label>
          <Form.Select
            value={form.tier ?? 'standard'}
            onChange={(e) => setForm({ ...form, tier: e.target.value as Client['tier'] })}
          >
            {enums?.client_tiers.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Form.Select>
          <Form.Text className="text-secondary">
            Not used yet — this is what SLA targets will key off when they arrive.
          </Form.Text>
        </Form.Group>

        <Form.Group>
          <Form.Label className="small fw-medium">Notes</Form.Label>
          <Form.Control
            as="textarea"
            rows={2}
            value={form.notes ?? ''}
            onChange={(e) => setForm({ ...form, notes: e.target.value })}
          />
        </Form.Group>
      </Modal.Body>
      <Modal.Footer>
        <Button variant="outline-secondary" size="sm" onClick={onClose}>
          Cancel
        </Button>
        <Button
          size="sm"
          disabled={!valid || save.isPending}
          onClick={async () => {
            await save.mutateAsync(form)
            onClose()
          }}
        >
          {save.isPending ? 'Saving…' : 'Save'}
        </Button>
      </Modal.Footer>
    </Modal>
  )
}
