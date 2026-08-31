import { Dropdown, Spinner } from 'react-bootstrap'
import { useNavigate } from 'react-router-dom'
import { useLogout } from '@/api/hooks/useAuth'
import { ROLE_LABELS, type RoleValue } from '@/lib/permissions'
import { useCurrentUser } from '@/lib/session'
import { useTheme, type Density, type ThemePreference } from '@/lib/theme'

const THEMES: { value: ThemePreference; label: string }[] = [
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
  { value: 'system', label: 'System' },
]

const DENSITIES: { value: Density; label: string }[] = [
  { value: 'comfortable', label: 'Comfortable' },
  { value: 'compact', label: 'Compact' },
]

export function UserMenu() {
  const user = useCurrentUser()
  const { theme, setTheme, density, setDensity } = useTheme()
  const logout = useLogout()
  const navigate = useNavigate()

  const initials = user.full_name
    .split(' ')
    .map((part) => part[0])
    .slice(0, 2)
    .join('')
    .toUpperCase()

  const roleLabel = user.is_platform_admin
    ? 'Platform Admin'
    : user.role
      ? ROLE_LABELS[user.role as RoleValue]
      : ''

  async function handleLogout() {
    await logout.mutateAsync()
    navigate('/login', { replace: true })
  }

  return (
    <Dropdown align="end">
      <Dropdown.Toggle
        variant="link"
        id="user-menu"
        className="d-flex align-items-center gap-2 text-decoration-none text-body px-2"
      >
        <span
          className="rounded-circle bg-secondary-subtle text-secondary-emphasis d-inline-grid"
          style={{
            width: 28,
            height: 28,
            fontSize: '0.7rem',
            fontWeight: 600,
            placeItems: 'center',
          }}
          aria-hidden="true"
        >
          {initials}
        </span>
        <span className="d-none d-md-inline small">{user.full_name}</span>
      </Dropdown.Toggle>

      <Dropdown.Menu style={{ minWidth: 250 }}>
        <div className="px-3 py-2">
          <div className="fw-semibold small">{user.full_name}</div>
          <div className="text-secondary" style={{ fontSize: '0.75rem' }}>
            {roleLabel}
            {user.teams.length > 0 && ` · ${user.teams.map((t) => t.name).join(', ')}`}
          </div>
        </div>

        <Dropdown.Divider />

        <Dropdown.Item onClick={() => navigate('/profile')}>👤 My Profile</Dropdown.Item>
        <Dropdown.Item onClick={() => navigate('/profile#password')}>
          🔑 Change Password
        </Dropdown.Item>

        <Dropdown.Divider />

        <Dropdown.Header className="text-uppercase" style={{ fontSize: '0.68rem' }}>
          Theme
        </Dropdown.Header>
        {THEMES.map((option) => (
          <Dropdown.Item
            key={option.value}
            active={theme === option.value}
            onClick={() => setTheme(option.value)}
          >
            {option.label}
          </Dropdown.Item>
        ))}

        <Dropdown.Header className="text-uppercase" style={{ fontSize: '0.68rem' }}>
          Density
        </Dropdown.Header>
        {DENSITIES.map((option) => (
          <Dropdown.Item
            key={option.value}
            active={density === option.value}
            onClick={() => setDensity(option.value)}
          >
            {option.label}
          </Dropdown.Item>
        ))}

        <Dropdown.Divider />
        <Dropdown.Item onClick={handleLogout} disabled={logout.isPending}>
          {logout.isPending ? (
            <>
              <Spinner as="span" animation="border" size="sm" className="me-2" />
              Signing out…
            </>
          ) : (
            '⏻ Log out'
          )}
        </Dropdown.Item>
      </Dropdown.Menu>
    </Dropdown>
  )
}
