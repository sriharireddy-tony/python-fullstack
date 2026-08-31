import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Page } from '@/api/client'
import { queryKeys } from '@/api/queryClient'
import type {
  Attachment,
  Comment,
  DashboardCounts,
  MetaEnums,
  Priority,
  StatusHistoryEntry,
  TicketDetail,
  TicketFilters,
  TicketListItem,
} from '@/api/types'

/** Enum metadata. Effectively static, so it is cached for the session. */
export function useMetaEnums() {
  return useQuery({
    queryKey: queryKeys.meta.enums,
    queryFn: () => api.get<MetaEnums>('/meta/enums'),
    staleTime: Infinity,
  })
}

export function useDashboardCounts() {
  return useQuery({
    queryKey: ['dashboard', 'counts'],
    queryFn: () => api.get<DashboardCounts>('/dashboard/counts'),
    staleTime: 30_000,
  })
}

export function useTickets(filters: TicketFilters) {
  return useQuery({
    queryKey: queryKeys.tickets.list(filters as Record<string, unknown>),
    queryFn: () =>
      api.get<Page<TicketListItem>>('/tickets', {
        query: filters as Record<string, string | number | boolean | string[] | undefined>,
      }),
    // Keeps the previous page visible while the next one loads, so the table
    // does not collapse to a spinner on every filter change.
    placeholderData: (previous) => previous,
  })
}

export function useTicket(reference: string | undefined) {
  return useQuery({
    queryKey: queryKeys.tickets.detail(reference ?? ''),
    queryFn: () => api.get<TicketDetail>(`/tickets/${reference}`),
    enabled: Boolean(reference),
  })
}

export function useTicketHistory(ticketId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.tickets.history(ticketId ?? ''),
    queryFn: () => api.get<StatusHistoryEntry[]>(`/tickets/${ticketId}/history`),
    enabled: Boolean(ticketId),
  })
}

export function usePriorityPreview() {
  return useMutation({
    mutationFn: (payload: { severity: string; impact: string; workaround: string }) =>
      api.post<{ priority: Priority; explanation: string }>('/tickets/priority-preview', payload),
  })
}

export function useCreateTicket() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      api.post<TicketDetail>('/tickets', payload, {
        // A CS agent will double-click. One key per form session means a retry
        // returns the original ticket instead of creating a duplicate.
        idempotencyKey: crypto.randomUUID(),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.tickets.all })
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    },
  })
}

/**
 * The nine ticket actions.
 *
 * All share one hook because they share one shape: post to an action endpoint
 * with the version, then refresh the ticket and any list that showed it.
 */
export function useTicketAction(reference: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ action, body }: { action: string; body: Record<string, unknown> }) =>
      api.post<TicketDetail>(`/tickets/${reference}/${action}`, body),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.tickets.detail(reference), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.tickets.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.tickets.history(updated.id) })
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    },
  })
}

export function useUpdateTicket(reference: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      api.patch<TicketDetail>(`/tickets/${reference}`, payload),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.tickets.detail(reference), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.tickets.all })
    },
  })
}

// ------------------------------------------------------------- comments

export function useComments(ticketId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.tickets.comments(ticketId ?? ''),
    queryFn: () => api.get<Comment[]>(`/tickets/${ticketId}/comments`),
    enabled: Boolean(ticketId),
  })
}

export function useAddComment(ticketId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: string) =>
      api.post<Comment>(`/tickets/${ticketId}/comments`, { body }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.tickets.comments(ticketId) }),
  })
}

export function useDeleteComment(ticketId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (commentId: string) => api.delete<void>(`/comments/${commentId}`),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.tickets.comments(ticketId) }),
  })
}

// ---------------------------------------------------------- attachments

export function useAttachments(ticketId: string | undefined) {
  return useQuery({
    queryKey: ['tickets', ticketId, 'attachments'],
    queryFn: () => api.get<Attachment[]>(`/tickets/${ticketId}/attachments`),
    enabled: Boolean(ticketId),
  })
}

export function useUploadAttachment(ticketId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData()
      form.append('file', file)
      // Sent through fetch directly: the JSON helper would set a Content-Type
      // that overrides the multipart boundary the browser generates.
      const csrf = document.cookie
        .split('; ')
        .find((c) => c.startsWith('csrf_token='))
        ?.split('=')[1]

      const response = await fetch(`/api/v1/tickets/${ticketId}/attachments`, {
        method: 'POST',
        body: form,
        credentials: 'include',
        headers: csrf ? { 'X-CSRF-Token': csrf } : undefined,
      })
      if (!response.ok) {
        const problem = (await response.json().catch(() => null)) as { detail?: string } | null
        throw new Error(problem?.detail ?? 'Upload failed')
      }
      return (await response.json()) as Attachment
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['tickets', ticketId, 'attachments'] }),
  })
}

export function useDeleteAttachment(ticketId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (attachmentId: string) => api.delete<void>(`/attachments/${attachmentId}`),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['tickets', ticketId, 'attachments'] }),
  })
}
