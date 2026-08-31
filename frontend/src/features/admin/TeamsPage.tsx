import { useState } from 'react'
import { Button, Card, Form, Modal, Table } from 'react-bootstrap'
import { ApiError } from '@/api/client'
import { useDeleteTeam, useSaveTeam, useTeams, useUsers } from '@/api/hooks/useAdmin'
import { EmptyState, ErrorState, LoadingState } from '@/components/states'
import { PageHeader } from '@/components/PageHeader'
import type { Team } from '@/api/types'

export function TeamsPage() {
  const { data: teams, isLoading, error, refetch } = useTeams()
  const { data: users } = useUsers({ activeOnly: true })
  const [editing, setEditing] = useState<Partial<Team> | null>(null)
  const remove = useDeleteTeam()
  const [removeError, setRemoveError] = useState<string | null>(null)

  const userName = (id: string | null) =>
    users?.items.find((u) => u.id === id)?.full_name ?? '—'

  return (
    <>
      <PageHeader
        title="Teams"
        subtitle="Engineering teams that own tickets. Stored as data, so adding one needs no deployment."
        actions={
          <Button size="sm" onClick={() => setEditing({})}>
            + Add team
          </Button>
        }
      />

      {removeError && <div className="alert alert-warning small">{removeError}</div>}
      {isLoading && <LoadingState />}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}

      {teams?.length === 0 && (
        <EmptyState
          title="No teams yet"
          description="Create the teams that own each product area — Platform, CoreHR, Payroll, and so on."
        />
      )}

      {teams && teams.length > 0 && (
        <Card>
          <Table hover responsive className="os-table mb-0 align-middle">
            <thead>
              <tr>
                <th>Name</th>
                <th style={{ width: 100 }}>Code</th>
                <th>Description</th>
                <th style={{ width: 160 }}>Manager</th>
                <th style={{ width: 90 }}>Members</th>
                <th style={{ width: 150 }} />
              </tr>
            </thead>
            <tbody>
              {teams.map((team) => (
                <tr key={team.id}>
                  <td>{team.name}</td>
                  <td className="font-monospace small">{team.code}</td>
                  <td className="small text-secondary">{team.description ?? '—'}</td>
                  <td className="small">{userName(team.manager_id)}</td>
                  <td className="small">{team.member_count}</td>
                  <td className="text-end">
                    <Button variant="link" size="sm" onClick={() => setEditing(team)}>
                      Edit
                    </Button>
                    {team.is_active && (
                      <Button
                        variant="link"
                        size="sm"
                        className="text-danger"
                        onClick={async () => {
                          setRemoveError(null)
                          try {
                            await remove.mutateAsync(team.id)
                          } catch (e) {
                            // The API refuses while the team still has open
                            // tickets — otherwise they would become
                            // unreachable from every inbox.
                            setRemoveError(
                              e instanceof ApiError ? e.message : 'Could not deactivate the team.',
                            )
                          }
                        }}
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

      {editing && <TeamModal team={editing} onClose={() => setEditing(null)} />}
    </>
  )
}

function TeamModal({ team, onClose }: { team: Partial<Team>; onClose: () => void }) {
  const save = useSaveTeam()
  const { data: users } = useUsers({ activeOnly: true })
  const [form, setForm] = useState<Partial<Team>>(team)
  const isNew = !team.id
  const valid = Boolean(form.name?.trim() && form.code?.trim())

  return (
    <Modal show onHide={onClose} centered>
      <Modal.Header closeButton>
        <Modal.Title className="h6">{isNew ? 'Add team' : 'Edit team'}</Modal.Title>
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
        </Form.Group>

        <Form.Group className="mb-3">
          <Form.Label className="small fw-medium">Description</Form.Label>
          <Form.Control
            value={form.description ?? ''}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="What area does this team own?"
          />
        </Form.Group>

        <Form.Group>
          <Form.Label className="small fw-medium">Manager</Form.Label>
          <Form.Select
            value={form.manager_id ?? ''}
            onChange={(e) => setForm({ ...form, manager_id: e.target.value || null })}
          >
            <option value="">No manager</option>
            {users?.items.map((u) => (
              <option key={u.id} value={u.id}>
                {u.full_name}
              </option>
            ))}
          </Form.Select>
          <Form.Text className="text-secondary">
            Owns the team inbox and can assign work within the team.
          </Form.Text>
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
