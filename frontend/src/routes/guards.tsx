import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { Spinner } from 'react-bootstrap'
import { hasPermission, type PermissionValue } from '@/lib/permissions'
import { usePermissions, useSession } from '@/lib/session'

function FullPageSpinner() {
  return (
    <div className="d-flex vh-100 align-items-center justify-content-center">
      <Spinner animation="border" />
    </div>
  )
}

/**
 * Gate for every authenticated route.
 *
 * The `from` location is preserved so a deep link survives the sign-in
 * round trip instead of dumping the user on the dashboard.
 */
export function RequireAuth() {
  const { isAuthenticated, isLoading } = useSession()
  const location = useLocation()

  if (isLoading) return <FullPageSpinner />
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }
  return <Outlet />
}

/**
 * Redirects rather than rendering a broken page when a deep link is not
 * permitted.
 *
 * UX only — the API returns 403 regardless of what the frontend rendered, and
 * row-level security scopes the data underneath that.
 */
export function RequirePermission({ permission }: { permission: PermissionValue }) {
  const permissions = usePermissions()
  const { isLoading } = useSession()

  if (isLoading) return <FullPageSpinner />
  if (!hasPermission(permissions, permission)) return <Navigate to="/forbidden" replace />
  return <Outlet />
}
