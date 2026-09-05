import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { SimilarTicketsResponse, SuggestionDecisionResult } from '@/api/types'

/**
 * Previously raised tickets that resemble this one.
 *
 * Fetched only on the ticket **detail** page, never on the create form. That
 * was a deliberate product decision: while a ticket is being typed there is
 * nothing to compare against yet, and a panel that reacts to every keystroke
 * is both expensive and distracting.
 *
 * `retry: false` because the endpoint answers 503 when Ollama or Chroma is
 * down. That is a real, final answer for this request — retrying three times
 * just delays showing the user an honest "unavailable" by several seconds.
 */
export function useSimilarTickets(reference: string | undefined, limit = 5) {
  return useQuery({
    queryKey: ['tickets', reference ?? '', 'similar', limit],
    queryFn: () =>
      api.get<SimilarTicketsResponse>(`/tickets/${reference}/similar`, {
        query: { limit },
      }),
    enabled: Boolean(reference),
    retry: false,
    // The corpus only changes when tickets are created or edited, so a result
    // stays good for a while. Five minutes keeps a page refresh free without
    // making a newly filed duplicate invisible for long.
    staleTime: 5 * 60_000,
  })
}

/**
 * Record agreement or disagreement with one suggestion.
 *
 * Optimistically updates nothing. A decision is cheap and the response is
 * fast, and an optimistic update here would mean showing "accepted" for a
 * click that failed a permission check — which is exactly the kind of quiet
 * lie that makes people stop trusting a panel.
 */
export function useDecideSuggestion(reference: string | undefined) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ suggestionId, accepted }: { suggestionId: string; accepted: boolean }) =>
      api.post<SuggestionDecisionResult>(`/tickets/suggestions/${suggestionId}/decide`, {
        accepted,
      }),
    onSuccess: () => {
      // Refetch rather than patching the cache: the server owns which
      // suggestions are still pending, and a decided one disappears from the
      // actionable set.
      void queryClient.invalidateQueries({ queryKey: ['tickets', reference ?? '', 'similar'] })
    },
  })
}
