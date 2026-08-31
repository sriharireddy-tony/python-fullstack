import { useState } from 'react'
import { Alert, Badge, Button, Card, Form, Modal, Table } from 'react-bootstrap'
import { ApiError } from '@/api/client'
import {
  useDeactivateUser,
  useReactivateUser,
  useSaveUser,
  useTeams,
  useUsers,
} from '@/api/hooks/useAdmin'
import { useMetaEnums } from '@/api/hooks/useTickets'
import { RelativeTime } from '@/components/badges'
import { ErrorState, LoadingState } from '@/components/states'
import { PageHeader } from '@/components/PageHeader'
import { ROLE_LABELS, type RoleValue } from '@/lib/permissions'
import type { AdminUser } from '@/api/types'

export function UsersPage() {
  const { data, isLoading, error, refetch } = useUsers()
  const { data: teams } = useTeams(true)
  const [editing, setEditing] = useState<Partial<AdminUser> | null>(null)
  const [deactivating, setDeactivating] = useState<AdminUser | null>(null)
  const reactivate = useReactivateUser()

  const teamNames = (ids: string[]) =>
    ids.map((id) => teams?.find((t) => t.id === id)?.name).filter(Boolean).join(', ') || '—'

  return (
    <>
      <PageHeader
        title="Users"
        subtitle="Accounts, roles, and team membership."
        actions={
          <Button size="sm" onClick={() => setEditing({})}>
            + Add user
          </Button>
        }
      />

      {isLoading && <LoadingState />}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}

      {data && (
        <Card>
          <Table hover responsive className="os-table mb-0 align-middle">
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th style={{ width: 130 }}>Role</th>
                <th style={{ width: 160 }}>Teams</th>
                <th style={{ width: 110 }}>Last login</th>
                <th style={{ width: 90 }}>Status</th>
                <th style={{ width: 160 }} />
              </tr>
            </thead>
            <tbody>
              {data.items.map((user) => (
                <tr key={user.id} className={user.is_active ? '' : 'opacity-50'}>
                  <td>{user.full_name}</td>
                  <td className="small text-secondary">{user.email}</td>
                  <td>
                    <Badge bg="secondary">
                      {ROLE_LABELS[user.role as RoleValue] ?? user.role}
                    </Badge>
                  </td>
                  <td className="small">{teamNames(user.team_ids)}</td>
                  <td className="small text-secondary">
                    {user.last_login_at ? <RelativeTime value={user.last_login_at} /> : 'Never'}
                  </td>
                  <td className="small">
                    {user.is_active ? (
                      <span className="text-success">Active</span>
                    ) : (
                      <span className="text-secondary">Inactive</span>
                    )}
                  </td>
                  <td className="text-end">
                    <Button variant="link" size="sm" onClick={() => setEditing(user)}>
                      Edit
                    </Button>
                    {user.is_active ? (
                      <Button
                        variant="link"
                        size="sm"
                        className="text-danger"
                        onClick={() => setDeactivating(user)}
                      >
                        Deactivate
                      </Button>
                    ) : (
                      <Button
                        variant="link"
                        size="sm"
                        onClick={() => void reactivate.mutateAsync(user.id)}
                      >
                        Reactivate
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}

      {editing && <UserModal user={editing} onClose={() => setEditing(null)} />}
      {deactivating && (
        <DeactivateModal user={deactivating} onClose={() => setDeactivating(null)} />
      )}
    </>
  )
}

function UserModal({ user, onClose }: { user: Partial<AdminUser>; onClose: () => void }) {
  const save = useSaveUser()
  const { data: teams } = useTeams(true)
  const { data: enums } = useMetaEnums()
  const [form, setForm] = useState<Record<string, unknown>>({
    role: 'developer',
    team_ids: [],
    ...user,
  })
  const [password, setPassword] = useState('')

  const isNew = !user.id
  const valid =
    Boolean((form.full_name as string)?.trim()) &&
    Boolean((form.email as string)?.trim()) &&
    (!isNew || password.length >= 12)

  return (
    <Modal show onHide={onClose} centered>
      <Modal.Header closeButton>
        <Modal.Title className="h6">{isNew ? 'Add user' : 'Edit user'}</Modal.Title>
      </Modal.Header>
      <Modal.Body>
        {save.isError && (
          <div className="alert alert-danger py-2 small">
            {save.error instanceof ApiError ? save.error.message : 'Could not save.'}
          </div>
        )}

        <Form.Group className="mb-3">
          <Form.Label className="small fw-medium">Full name</Form.Label>
          <Form.Control
            value={(form.full_name as string) ?? ''}
            onChange={(e) => setForm({ ...form, full_name: e.target.value })}
          />
        </Form.Group>

        <Form.Group className="mb-3">
          <Form.Label className="small fw-medium">Email</Form.Label>
          <Form.Control
            type="email"
            value={(form.email as string) ?? ''}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
            disabled={!isNew}
          />
        </Form.Group>

        {isNew && (
          <Form.Group className="mb-3">
            <Form.Label className="small fw-medium">Initial password</Form.Label>
            <Form.Control
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <Form.Text className="text-secondary">
              At least 12 characters. Length is the only rule — the user can change it after
              signing in.
            </Form.Text>
          </Form.Group>
        )}

        <Form.Group className="mb-3">
          <Form.Label className="small fw-medium">Role</Form.Label>
          <Form.Select
            value={(form.role as string) ?? 'developer'}
            onChange={(e) => setForm({ ...form, role: e.target.value })}
          >
            {enums?.roles.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Form.Select>
        </Form.Group>

        <Form.Group>
          <Form.Label className="small fw-medium">Teams</Form.Label>
          <div className="d-flex flex-wrap gap-3">
            {teams?.map((team) => {
              const selected = ((form.team_ids as string[]) ?? []).includes(team.id)
              return (
                <Form.Check
                  key={team.id}
                  type="checkbox"
                  id={`team-${team.id}`}
                  label={team.name}
                  checked={selected}
                  onChange={() => {
                    const current = (form.team_ids as string[]) ?? []
                    setForm({
                      ...form,
                      team_ids: selected
                        ? current.filter((id) => id !== team.id)
                        : [...current, team.id],
                    })
                  }}
                />
              )
            })}
          </div>
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
            await save.mutateAsync(isNew ? { ...form, password } : form)
            onClose()
          }}
        >
          {save.isPending ? 'Saving…' : 'Save'}
        </Button>
      </Modal.Footer>
    </Modal>
  )
}

/**
 * Deactivation prompts for reassignment.
 *
 * Setting `is_active = false` hides the user but leaves their tickets assigned
 * to an account nobody can act as. Without this step those tickets become a
 * silent black hole that nobody notices until a client asks.
 */
function DeactivateModal({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const deactivate = useDeactivateUser()
  const { data: users } = useUsers({ activeOnly: true })
  const [reassignTo, setReassignTo] = useState<string>('')
  const [result, setResult] = useState<{ tickets_reassigned: number } | null>(null)

  const candidates = users?.items.filter((u) => u.id !== user.id) ?? []

  return (
    <Modal show onHide={onClose} centered>
      <Modal.Header closeButton>
        <Modal.Title className="h6">Deactivate {user.full_name}</Modal.Title>
      </Modal.Header>
      <Modal.Body>
        {result ? (
          <Alert variant="success" className="small mb-0">
            Account deactivated. {result.tickets_reassigned} open ticket
            {result.tickets_reassigned === 1 ? '' : 's'} handed over, and all their sessions
            were ended.
          </Alert>
        ) : (
          <>
            <Alert variant="warning" className="small">
              Their sessions end immediately. Any open tickets still assigned to them need a new
              owner, or they will be invisible to everyone.
            </Alert>

            {deactivate.isError && (
              <div className="alert alert-danger py-2 small">
                {deactivate.error instanceof ApiError
                  ? deactivate.error.message
                  : 'Could not deactivate.'}
              </div>
            )}

            <Form.Group>
              <Form.Label className="small fw-medium">Hand their open tickets to</Form.Label>
              <Form.Select value={reassignTo} onChange={(e) => setReassignTo(e.target.value)}>
                <option value="">Return them to the team inbox (unassigned)</option>
                {candidates.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.full_name}
                  </option>
                ))}
              </Form.Select>
            </Form.Group>
          </>
        )}
      </Modal.Body>
      <Modal.Footer>
        <Button variant="outline-secondary" size="sm" onClick={onClose}>
          {result ? 'Done' : 'Cancel'}
        </Button>
        {!result && (
          <Button
            variant="danger"
            size="sm"
            disabled={deactivate.isPending}
            onClick={async () => {
              const outcome = await deactivate.mutateAsync({
                id: user.id,
                reassignTo: reassignTo || null,
              })
              setResult(outcome)
            }}
          >
            {deactivate.isPending ? 'Working…' : 'Deactivate'}
          </Button>
        )}
      </Modal.Footer>
    </Modal>
  )
}
