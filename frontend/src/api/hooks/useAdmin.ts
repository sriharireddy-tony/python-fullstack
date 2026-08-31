import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Page } from '@/api/client'
import { queryKeys } from '@/api/queryClient'
import type { AdminUser, AuditEntry, Client, Team } from '@/api/types'

// ------------------------------------------------------------- clients

export function useClients(options: { activeOnly?: boolean; q?: string } = {}) {
  return useQuery({
    queryKey: [...queryKeys.clients.all, options],
    queryFn: () =>
      api.get<Page<Client>>('/clients', {
        query: { active_only: options.activeOnly, q: options.q, page_size: 100 },
      }),
    staleTime: 5 * 60_000,
  })
}

export function useSaveClient() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: Partial<Client> & { id?: string }) =>
      id
        ? api.patch<Client>(`/clients/${id}`, body)
        : api.post<Client>('/clients', body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.clients.all }),
  })
}

export function useDeleteClient() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/clients/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.clients.all }),
  })
}

// --------------------------------------------------------------- teams

export function useTeams(activeOnly = false) {
  return useQuery({
    queryKey: [...queryKeys.teams.all, { activeOnly }],
    queryFn: () => api.get<Team[]>('/teams', { query: { active_only: activeOnly } }),
    staleTime: 5 * 60_000,
  })
}

export function useSaveTeam() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: Partial<Team> & { id?: string }) =>
      id ? api.patch<Team>(`/teams/${id}`, body) : api.post<Team>('/teams', body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.teams.all }),
  })
}

export function useDeleteTeam() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/teams/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.teams.all }),
  })
}

// --------------------------------------------------------------- users

export function useUsers(options: { activeOnly?: boolean; teamId?: string; q?: string } = {}) {
  return useQuery({
    queryKey: [...queryKeys.users.all, options],
    queryFn: () =>
      api.get<Page<AdminUser>>('/users', {
        query: {
          active_only: options.activeOnly,
          team_id: options.teamId,
          q: options.q,
          page_size: 100,
        },
      }),
    staleTime: 60_000,
  })
}

export function useSaveUser() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: Record<string, unknown> & { id?: string }) =>
      id
        ? api.patch<AdminUser>(`/users/${id}`, body)
        : api.post<AdminUser>('/users', body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.users.all }),
  })
}

/**
 * Deactivation is not just a flag: the user's open tickets must go somewhere,
 * or they become invisible to everyone.
 */
export function useDeactivateUser() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, reassignTo }: { id: string; reassignTo: string | null }) =>
      api.post<{ tickets_reassigned: number; sessions_revoked: number }>(
        `/users/${id}/deactivate`,
        { reassign_tickets_to: reassignTo },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.users.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.tickets.all })
    },
  })
}

export function useReactivateUser() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.post<AdminUser>(`/users/${id}/reactivate`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.users.all }),
  })
}

// --------------------------------------------------------------- audit

export function useAuditLog(options: { entityType?: string; page?: number } = {}) {
  return useQuery({
    queryKey: ['audit', options],
    queryFn: () =>
      api.get<Page<AuditEntry>>('/audit-logs', {
        query: { entity_type: options.entityType, page: options.page ?? 1, page_size: 50 },
      }),
  })
}
