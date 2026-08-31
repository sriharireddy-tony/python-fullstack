/**
 * Session context.
 *
 * Backed by `GET /api/v1/auth/me`. The permission list comes from the server,
 * so the UI adapts to a role change without a frontend deployment.
 *
 * Everything read from here is UX only. The API enforces every permission
 * independently, and row-level security scopes the data underneath that.
 */

import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { useSessionQuery, type SessionUser } from '@/api/hooks/useAuth'
import type { RoleValue } from './permissions'

interface SessionContextValue {
  user: SessionUser | null
  isAuthenticated: boolean
  isLoading: boolean
  permissions: string[]
  role: RoleValue | null
}

const SessionContext = createContext<SessionContextValue | null>(null)

export function SessionProvider({ children }: { children: ReactNode }) {
  const { data, isLoading } = useSessionQuery()

  const value = useMemo<SessionContextValue>(
    () => ({
      user: data ?? null,
      isAuthenticated: Boolean(data),
      isLoading,
      permissions: data?.permissions ?? [],
      role: (data?.role as RoleValue | null) ?? null,
    }),
    [data, isLoading],
  )

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext)
  if (!ctx) throw new Error('useSession must be used inside <SessionProvider>')
  return ctx
}

/** Permissions of the current user. Empty when signed out. */
export function usePermissions(): string[] {
  return useSession().permissions
}

/** The signed-in user, or throw. Use only below an auth guard. */
export function useCurrentUser(): SessionUser {
  const { user } = useSession()
  if (!user) throw new Error('useCurrentUser used outside an authenticated route')
  return user
}
