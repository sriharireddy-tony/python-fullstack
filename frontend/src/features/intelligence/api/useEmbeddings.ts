import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type {
  DeleteEmbeddingsOutcome,
  EmbedOutcome,
  EmbeddingListResponse,
  EmbeddingState,
  EmbeddingSummary,
} from '@/api/types'

export interface EmbeddingFilters {
  team_id?: string
  status?: string
  state?: EmbeddingState
  search?: string
  limit?: number
  offset?: number
}

/**
 * Tickets and their index status.
 *
 * `staleTime: 0` — the opposite of the similar-issues hook, and deliberately
 * so. This page exists to report what is currently indexed, and a cached view
 * of that after the operator just embedded something is precisely the lie the
 * page exists to prevent.
 */
export function useEmbeddingList(filters: EmbeddingFilters) {
  return useQuery({
    queryKey: ['embeddings', 'list', filters],
    queryFn: () =>
      api.get<EmbeddingListResponse>('/ai/embeddings', {
        query: {
          team_id: filters.team_id,
          status: filters.status,
          state: filters.state,
          search: filters.search,
          limit: filters.limit ?? 50,
          offset: filters.offset ?? 0,
        },
      }),
    staleTime: 0,
  })
}

/**
 * Totals, including what the vector store itself holds.
 *
 * The store count is the number worth watching: Postgres reports what it
 * *recorded*, and only the store knows what it *has*. When they disagree, that
 * gap is the drift the console exists to make visible.
 */
export function useEmbeddingSummary() {
  return useQuery({
    queryKey: ['embeddings', 'summary'],
    queryFn: () => api.get<EmbeddingSummary>('/ai/embeddings/summary'),
    staleTime: 0,
    // The vector store being unreachable is a real, final answer for this
    // request. Retrying delays showing an honest "unavailable" by seconds.
    retry: false,
  })
}

/**
 * Embed the selected tickets, now.
 *
 * The request blocks until the work is done, because the whole point of the
 * console is that a person watching gets told what happened rather than a job
 * id to go and chase.
 */
export function useEmbedTickets() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ ticketIds, force }: { ticketIds: string[]; force: boolean }) =>
      api.post<EmbedOutcome>('/ai/embeddings/embed', {
        ticket_ids: ticketIds,
        force,
      }),
    onSuccess: () => {
      // Invalidate both, not just the list: the summary's store count is the
      // number the operator is checking, and leaving it stale would show a
      // freshly embedded ticket against an unchanged total.
      void queryClient.invalidateQueries({ queryKey: ['embeddings'] })
    },
  })
}

/** Remove vectors for the selected tickets. The tickets are untouched. */
export function useDeleteEmbeddings() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (ticketIds: string[]) =>
      api.post<DeleteEmbeddingsOutcome>('/ai/embeddings/delete', {
        ticket_ids: ticketIds,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['embeddings'] })
    },
  })
}
