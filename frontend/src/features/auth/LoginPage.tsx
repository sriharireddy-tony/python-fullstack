import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Alert, Button, Card, Form, Spinner } from 'react-bootstrap'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { ApiError, NetworkError } from '@/api/client'
import { useLogin } from '@/api/hooks/useAuth'
import { useSession } from '@/lib/session'

const schema = z.object({
  email: z.string().min(1, 'Enter your email').email('Enter a valid email address'),
  password: z.string().min(1, 'Enter your password'),
})

type FormValues = z.infer<typeof schema>

export function LoginPage() {
  const { isAuthenticated, isLoading } = useSession()
  const login = useLogin()
  const navigate = useNavigate()
  const location = useLocation()

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({ resolver: zodResolver(schema) })

  if (isLoading) {
    return (
      <div className="d-flex vh-100 align-items-center justify-content-center">
        <Spinner animation="border" />
      </div>
    )
  }

  if (isAuthenticated) return <Navigate to="/" replace />

  async function onSubmit(values: FormValues) {
    await login.mutateAsync(values)
    // Return the user to wherever they were headed before the redirect.
    const from = (location.state as { from?: string } | null)?.from ?? '/'
    navigate(from, { replace: true })
  }

  return (
    <div className="d-flex vh-100 align-items-center justify-content-center bg-body-tertiary">
      <Card style={{ width: '100%', maxWidth: 400 }} className="shadow-sm">
        <Card.Body className="p-4">
          <div className="d-flex align-items-center gap-2 mb-1">
            <span className="os-brand-mark">OS</span>
            <span className="fw-semibold fs-5">OS Tracker</span>
          </div>
          <p className="text-secondary small mb-4">Operational Support Tracker</p>

          {login.isError && <LoginError error={login.error} />}

          <Form onSubmit={handleSubmit(onSubmit)} noValidate>
            <Form.Group className="mb-3" controlId="email">
              <Form.Label className="small fw-medium">Email</Form.Label>
              <Form.Control
                type="email"
                autoComplete="username"
                autoFocus
                isInvalid={Boolean(errors.email)}
                {...register('email')}
              />
              <Form.Control.Feedback type="invalid">
                {errors.email?.message}
              </Form.Control.Feedback>
            </Form.Group>

            <Form.Group className="mb-4" controlId="password">
              <Form.Label className="small fw-medium">Password</Form.Label>
              <Form.Control
                type="password"
                autoComplete="current-password"
                isInvalid={Boolean(errors.password)}
                {...register('password')}
              />
              <Form.Control.Feedback type="invalid">
                {errors.password?.message}
              </Form.Control.Feedback>
            </Form.Group>

            <Button type="submit" variant="primary" className="w-100" disabled={isSubmitting}>
              {isSubmitting ? (
                <>
                  <Spinner as="span" animation="border" size="sm" className="me-2" />
                  Signing in…
                </>
              ) : (
                'Sign in'
              )}
            </Button>
          </Form>
        </Card.Body>
      </Card>
    </div>
  )
}

/**
 * Authentication failures show the server's single generic message on purpose.
 * Distinguishing "no such account" from "wrong password" would confirm which
 * email addresses exist.
 */
function LoginError({ error }: { error: unknown }) {
  let message = 'Something went wrong. Try again.'
  if (error instanceof NetworkError) message = error.message
  else if (error instanceof ApiError) message = error.message

  return (
    <Alert variant="danger" className="py-2 small">
      {message}
    </Alert>
  )
}
