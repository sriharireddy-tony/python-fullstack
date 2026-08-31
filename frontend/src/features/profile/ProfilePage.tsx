import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Alert, Button, Card, Col, Form, Row } from 'react-bootstrap'
import { ApiError } from '@/api/client'
import { useChangePassword, useUpdateProfile } from '@/api/hooks/useAuth'
import { PageHeader } from '@/components/PageHeader'
import { ROLE_LABELS, type RoleValue } from '@/lib/permissions'
import { useCurrentUser } from '@/lib/session'

const profileSchema = z.object({
  full_name: z.string().min(1, 'Enter your name').max(200),
})

const passwordSchema = z
  .object({
    current_password: z.string().min(1, 'Enter your current password'),
    // Mirrors the server rule: length only, no composition requirements.
    // Composition rules push people towards predictable substitutions.
    new_password: z.string().min(12, 'Use at least 12 characters'),
    confirm_password: z.string(),
  })
  .refine((v) => v.new_password === v.confirm_password, {
    message: 'Passwords do not match',
    path: ['confirm_password'],
  })
  .refine((v) => v.new_password !== v.current_password, {
    message: 'The new password must differ from the current one',
    path: ['new_password'],
  })

export function ProfilePage() {
  const user = useCurrentUser()
  const updateProfile = useUpdateProfile()
  const changePassword = useChangePassword()
  const [profileSaved, setProfileSaved] = useState(false)

  const profileForm = useForm<z.infer<typeof profileSchema>>({
    resolver: zodResolver(profileSchema),
    defaultValues: { full_name: user.full_name },
  })

  const passwordForm = useForm<z.infer<typeof passwordSchema>>({
    resolver: zodResolver(passwordSchema),
  })

  return (
    <>
      <PageHeader title="My Profile" subtitle={user.email} />

      <Row className="g-3">
        <Col lg={6}>
          <Card>
            <Card.Header className="fw-semibold small">Details</Card.Header>
            <Card.Body>
              {profileSaved && (
                <Alert variant="success" className="py-2 small">
                  Profile updated.
                </Alert>
              )}
              <Form
                onSubmit={profileForm.handleSubmit(async (values) => {
                  await updateProfile.mutateAsync(values)
                  setProfileSaved(true)
                })}
              >
                <Form.Group className="mb-3">
                  <Form.Label className="small fw-medium">Full name</Form.Label>
                  <Form.Control
                    isInvalid={Boolean(profileForm.formState.errors.full_name)}
                    {...profileForm.register('full_name')}
                  />
                  <Form.Control.Feedback type="invalid">
                    {profileForm.formState.errors.full_name?.message}
                  </Form.Control.Feedback>
                </Form.Group>

                <Form.Group className="mb-3">
                  <Form.Label className="small fw-medium">Email</Form.Label>
                  <Form.Control value={user.email} disabled readOnly />
                  <Form.Text className="text-secondary">
                    Contact an administrator to change your email address.
                  </Form.Text>
                </Form.Group>

                <Row className="mb-3">
                  <Col>
                    <Form.Label className="small fw-medium">Role</Form.Label>
                    <div className="small">
                      {user.role ? ROLE_LABELS[user.role as RoleValue] : 'Platform Admin'}
                    </div>
                  </Col>
                  <Col>
                    <Form.Label className="small fw-medium">Teams</Form.Label>
                    <div className="small">
                      {user.teams.length ? user.teams.map((t) => t.name).join(', ') : '—'}
                    </div>
                  </Col>
                </Row>

                <Button
                  type="submit"
                  size="sm"
                  disabled={profileForm.formState.isSubmitting}
                >
                  Save changes
                </Button>
              </Form>
            </Card.Body>
          </Card>
        </Col>

        <Col lg={6}>
          <Card>
            <Card.Header className="fw-semibold small">Change password</Card.Header>
            <Card.Body>
              <Alert variant="warning" className="py-2 small">
                Changing your password signs you out of every device, including this one.
              </Alert>

              {changePassword.isError && (
                <Alert variant="danger" className="py-2 small">
                  {changePassword.error instanceof ApiError
                    ? changePassword.error.message
                    : 'Could not change the password.'}
                </Alert>
              )}

              <Form
                onSubmit={passwordForm.handleSubmit(async (values) => {
                  await changePassword.mutateAsync({
                    current_password: values.current_password,
                    new_password: values.new_password,
                  })
                  // The session is gone; the auth guard sends us to /login.
                })}
              >
                {(
                  [
                    ['current_password', 'Current password', 'current-password'],
                    ['new_password', 'New password', 'new-password'],
                    ['confirm_password', 'Confirm new password', 'new-password'],
                  ] as const
                ).map(([name, label, autoComplete]) => (
                  <Form.Group className="mb-3" key={name}>
                    <Form.Label className="small fw-medium">{label}</Form.Label>
                    <Form.Control
                      type="password"
                      autoComplete={autoComplete}
                      isInvalid={Boolean(passwordForm.formState.errors[name])}
                      {...passwordForm.register(name)}
                    />
                    <Form.Control.Feedback type="invalid">
                      {passwordForm.formState.errors[name]?.message}
                    </Form.Control.Feedback>
                  </Form.Group>
                ))}

                <Button
                  type="submit"
                  size="sm"
                  variant="warning"
                  disabled={passwordForm.formState.isSubmitting}
                >
                  Change password
                </Button>
              </Form>
            </Card.Body>
          </Card>
        </Col>
      </Row>
    </>
  )
}
