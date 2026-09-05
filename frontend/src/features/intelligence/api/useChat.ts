import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { ChatMessage, ChatTurn, Conversation } from '@/api/types'

const keys = {
  all: ['chat'] as const,
  conversations: ['chat', 'conversations'] as const,
  messages: (id: string) => ['chat', id, 'messages'] as const,
}

export function useConversations() {
  return useQuery({
    queryKey: keys.conversations,
    queryFn: () => api.get<Conversation[]>('/chat'),
  })
}

/**
 * The transcript, read from the server.
 *
 * The server reads it back from the LangGraph checkpointer rather than from a
 * table of its own, which is why this is a query rather than local state: the
 * conversation lives in Postgres, and the browser is just a view of it. Open
 * the same thread in another tab and it is there.
 */
export function useMessages(conversationId: string | undefined) {
  return useQuery({
    queryKey: keys.messages(conversationId ?? ''),
    queryFn: () => api.get<ChatMessage[]>(`/chat/${conversationId}/messages`),
    enabled: Boolean(conversationId),
  })
}

export function useStartConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<Conversation>('/chat', {}),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.conversations })
    },
  })
}

export function useSendMessage(conversationId: string | undefined) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (message: string) =>
      api.post<ChatTurn>(`/chat/${conversationId}/messages`, { message }),
    onSuccess: () => {
      // Refetch the transcript rather than appending locally. The server owns
      // it, and a locally-appended message that the server did not record is
      // the kind of divergence nobody notices until a refresh loses it.
      void queryClient.invalidateQueries({ queryKey: keys.messages(conversationId ?? '') })
      void queryClient.invalidateQueries({ queryKey: keys.conversations })
    },
  })
}

export function useDeleteConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (conversationId: string) => api.delete<void>(`/chat/${conversationId}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.conversations })
    },
  })
}
