import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { AnalysisRun } from '@/api/types'

/** Statuses that mean the run is over and polling should stop. */
const TERMINAL = new Set(['completed', 'failed', 'budget_exceeded', 'timed_out'])

/**
 * The latest analysis for a ticket, polled while it runs.
 *
 * `refetchInterval` returns false once the run is terminal, so polling stops
 * on its own rather than needing an effect to clear it. Two seconds while
 * running: the step log grows about that fast, and the whole reason to poll
 * rather than wait is that the user sees the agent working.
 */
export function useAnalysis(reference: string | undefined) {
  return useQuery({
    queryKey: ['tickets', reference ?? '', 'analysis'],
    queryFn: () => api.get<AnalysisRun | null>(`/tickets/${reference}/analysis`),
    enabled: Boolean(reference),
    refetchInterval: (query) => {
      const run = query.state.data
      if (!run || TERMINAL.has(run.status)) return false
      return 2000
    },
  })
}

export function useRequestAnalysis(reference: string | undefined) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<AnalysisRun>(`/tickets/${reference}/analysis`, {}),
    onSuccess: (run) => {
      // Seed the cache with the queued run so polling starts immediately,
      // rather than waiting a refetch to discover what we already know.
      queryClient.setQueryData(['tickets', reference ?? '', 'analysis'], run)
    },
  })
}

export function useRateAnalysis(reference: string | undefined) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ runId, helpful }: { runId: string; helpful: boolean }) =>
      api.post<AnalysisRun>(`/tickets/analysis/${runId}/rating`, { helpful }),
    onSuccess: (run) => {
      queryClient.setQueryData(['tickets', reference ?? '', 'analysis'], run)
    },
  })
}
