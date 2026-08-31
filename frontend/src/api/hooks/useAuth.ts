import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '@/api/client'
import { queryKeys } from '@/api/queryClient'

export interface TeamSummary {
  id: string
  name: string
}

export interface SessionUser {
  id: string
  email: string
  full_name: string
  role: string | null
  is_platform_admin: boolean
  permissions: string[]
  teams: TeamSummary[]
  preferences: { theme: string; density: string }
  tenant_id: string | null
  last_login_at: string | null
}

/**
 * The current session.
 *
 * A 401 is a legitimate answer here, not an error to retry or surface — it
 * simply means "not signed in". Treating it as a normal `null` keeps the login
 * redirect out of the error path.
 */
export function useSessionQuery() {
  return useQuery({
    queryKey: queryKeys.session,
    queryFn: async (): Promise<SessionUser | null> => {
      try {
        return await api.get<SessionUser>('/auth/me')
      } catch (error) {
        if (error instanceof ApiError && error.isAuthError) return null
        throw error
      }
    },
    staleTime: 60_000,
    retry: false,
  })
}

export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (credentials: { email: string; password: string }) =>
      api.post<SessionUser>('/auth/login', credentials),
    onSuccess: (session) => {
      queryClient.setQueryData(queryKeys.session, session)
    },
  })
}

export function useLogout() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<void>('/auth/logout'),
    // Clear on settled rather than on success: if logout fails server-side the
    // local session is still gone, and leaving stale data on screen would be
    // worse than an extra sign-in.
    onSettled: () => {
      queryClient.setQueryData(queryKeys.session, null)
      queryClient.clear()
    },
  })
}

export function useChangePassword() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: { current_password: string; new_password: string }) =>
      api.post<void>('/auth/change-password', payload),
    onSuccess: () => {
      // Changing the password revokes every session, including this one.
      queryClient.setQueryData(queryKeys.session, null)
      queryClient.clear()
    },
  })
}

export function useUpdateProfile() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: { full_name: string }) =>
      api.patch<SessionUser>('/auth/me/profile', payload),
    onSuccess: (session) => queryClient.setQueryData(queryKeys.session, session),
  })
}

export function useUpdatePreferences() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: { theme?: string; density?: string }) =>
      api.patch<SessionUser>('/auth/me/preferences', payload),
    onSuccess: (session) => queryClient.setQueryData(queryKeys.session, session),
  })
}
