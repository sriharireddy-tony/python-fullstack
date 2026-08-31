import { QueryClient } from '@tanstack/react-query'
import { ApiError } from './client'

/**
 * Shared query client.
 *
 * The retry rule is the important part: retrying a 401, 403, 404, or a
 * validation failure is pointless -- the answer will not change -- and it
 * delays showing the user what actually happened. Only genuinely transient
 * failures are retried.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: true,
      retry: (failureCount, error) => {
        if (error instanceof ApiError) {
          if (error.status >= 400 && error.status < 500) return false
        }
        return failureCount < 2
      },
    },
    mutations: {
      // Mutations are never retried automatically. A retried close or reject
      // could act twice; the user decides whether to try again.
      retry: false,
    },
  },
})

/**
 * Query key factory.
 *
 * Centralised so invalidation targets the narrowest key that could have
 * changed, instead of every call site inventing its own key shape.
 */
export const queryKeys = {
  session: ['session'] as const,
  tickets: {
    all: ['tickets'] as const,
    list: (filters: Record<string, unknown>) => ['tickets', 'list', filters] as const,
    detail: (id: string) => ['tickets', id] as const,
    comments: (id: string) => ['tickets', id, 'comments'] as const,
    history: (id: string) => ['tickets', id, 'history'] as const,
  },
  clients: { all: ['clients'] as const },
  teams: { all: ['teams'] as const },
  users: { all: ['users'] as const },
  meta: { enums: ['meta', 'enums'] as const },
} as const
