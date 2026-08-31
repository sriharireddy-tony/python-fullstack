/**
 * The three states every list and page must handle.
 *
 * Decided once here so no screen invents its own version, and so "what does an
 * empty ticket list look like?" is never an open question mid-feature.
 * See docs/07-frontend.md.
 */

import { Alert, Button, Spinner } from 'react-bootstrap'
import type { ReactNode } from 'react'
import { ApiError, NetworkError } from '@/api/client'

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="os-state" role="status" aria-live="polite">
      <Spinner animation="border" size="sm" />
      <div className="small">{label}</div>
    </div>
  )
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="os-state">
      <div style={{ fontSize: '1.75rem', opacity: 0.5 }} aria-hidden="true">
        ∅
      </div>
      <div className="fw-semibold text-body">{title}</div>
      {description && <div className="small" style={{ maxWidth: 420 }}>{description}</div>}
      {action}
    </div>
  )
}

/**
 * Error display.
 *
 * Shows the request id when there is one -- it is the string that finds the
 * exact request in the backend logs, so quoting it turns "it failed" into a
 * diagnosable report.
 */
export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { title, message, requestId } = describeError(error)

  return (
    <Alert variant="danger" className="my-3">
      <Alert.Heading className="h6">{title}</Alert.Heading>
      <p className="mb-2 small">{message}</p>
      {requestId && (
        <p className="mb-2 small text-body-secondary font-monospace">
          Reference: {requestId}
        </p>
      )}
      {onRetry && (
        <Button size="sm" variant="outline-danger" onClick={onRetry}>
          Try again
        </Button>
      )}
    </Alert>
  )
}

function describeError(error: unknown): {
  title: string
  message: string
  requestId?: string
} {
  if (error instanceof NetworkError) {
    return { title: 'Cannot reach the server', message: error.message }
  }

  if (error instanceof ApiError) {
    if (error.isAuthError) {
      return { title: 'Session expired', message: 'Sign in again to continue.' }
    }
    if (error.isPermissionError) {
      return {
        title: 'Not permitted',
        message: 'You do not have access to this. Ask an administrator if you think this is wrong.',
        requestId: error.requestId,
      }
    }
    if (error.isVersionConflict) {
      return {
        title: 'This was changed by someone else',
        message: 'Reload to see the latest version before making your change.',
        requestId: error.requestId,
      }
    }
    return { title: 'Something went wrong', message: error.message, requestId: error.requestId }
  }

  return {
    title: 'Something went wrong',
    message: 'An unexpected error occurred. Try again in a moment.',
  }
}
