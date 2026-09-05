import { useEffect, useRef, useState } from 'react'
import { Alert, Badge, Button, Card, Col, Form, ListGroup, Row, Spinner } from 'react-bootstrap'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/states'
import type { ChatMessage } from '@/api/types'
import {
  useConversations,
  useDeleteConversation,
  useMessages,
  useSendMessage,
  useStartConversation,
} from '../api/useChat'

const SUGGESTIONS = [
  'How many tickets does the Payroll team have open?',
  'Which team has the most reopened tickets?',
  'Has the payslip PDF problem been reported before?',
  'What is Priya working on?',
]

/**
 * The assistant.
 *
 * Two decisions worth naming.
 *
 * **The transcript is server state, not component state.** It is read back
 * from the LangGraph checkpointer, so the same conversation is there after a
 * refresh, in another tab, or on another machine. Appending replies locally
 * would be faster to write and would drift from what was actually persisted.
 *
 * **Threads are private to whoever created them.** The list only ever contains
 * the caller's own, enforced server-side. A question someone asks an assistant
 * is closer to a search history than to a ticket comment.
 */
export function ChatPage() {
  const { data: conversations, isLoading } = useConversations()
  const [activeId, setActiveId] = useState<string | null>(null)
  const start = useStartConversation()
  const remove = useDeleteConversation()

  // Open the most recent thread on arrival, so the page is never an empty
  // shell when there is something to read.
  useEffect(() => {
    const first = conversations?.[0]
    if (!activeId && first) {
      setActiveId(first.id)
    }
  }, [activeId, conversations])

  const onStart = async () => {
    const created = await start.mutateAsync()
    setActiveId(created.id)
  }

  return (
    <>
      <PageHeader
        title="Assistant"
        subtitle="Ask about the tickets in this workspace. It can read them; it cannot change them."
        actions={
          <Button size="sm" onClick={() => void onStart()} disabled={start.isPending}>
            New conversation
          </Button>
        }
      />

      <Row className="g-3">
        <Col lg={3}>
          <Card>
            <Card.Header className="fw-semibold small">Your conversations</Card.Header>
            {isLoading && (
              <Card.Body className="small text-secondary">
                <Spinner animation="border" size="sm" className="me-2" />
                Loading…
              </Card.Body>
            )}
            {conversations && conversations.length === 0 && (
              <Card.Body className="small text-secondary">
                Nothing yet. Start one to ask a question.
              </Card.Body>
            )}
            <ListGroup variant="flush">
              {conversations?.map((conversation) => (
                <ListGroup.Item
                  key={conversation.id}
                  action
                  active={conversation.id === activeId}
                  onClick={() => setActiveId(conversation.id)}
                  className="small d-flex justify-content-between align-items-start gap-2"
                >
                  <span className="text-truncate">{conversation.title ?? 'Conversation'}</span>
                  <Button
                    variant="link"
                    size="sm"
                    className="p-0 text-danger"
                    style={{ fontSize: '0.7rem' }}
                    onClick={(event) => {
                      event.stopPropagation()
                      void remove.mutateAsync(conversation.id).then(() => {
                        if (conversation.id === activeId) setActiveId(null)
                      })
                    }}
                  >
                    delete
                  </Button>
                </ListGroup.Item>
              ))}
            </ListGroup>
          </Card>
        </Col>

        <Col lg={9}>
          {activeId ? (
            <Thread conversationId={activeId} />
          ) : (
            <Card>
              <Card.Body>
                <EmptyState
                  title="No conversation open"
                  description="Start a new one to ask about tickets, duplicates, or who is working on what."
                />
                <div className="d-flex justify-content-center">
                  <Button size="sm" onClick={() => void onStart()} disabled={start.isPending}>
                    New conversation
                  </Button>
                </div>
              </Card.Body>
            </Card>
          )}
        </Col>
      </Row>
    </>
  )
}

function Thread({ conversationId }: { conversationId: string }) {
  const { data: messages, isLoading } = useMessages(conversationId)
  const send = useSendMessage(conversationId)
  const [draft, setDraft] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, send.isPending])

  const submit = async (text: string) => {
    const message = text.trim()
    if (!message || send.isPending) return
    setDraft('')
    await send.mutateAsync(message)
  }

  const turn = send.data

  return (
    <Card>
      <Card.Body style={{ minHeight: 420, maxHeight: '60vh', overflowY: 'auto' }}>
        {isLoading && (
          <div className="small text-secondary">
            <Spinner animation="border" size="sm" className="me-2" />
            Loading…
          </div>
        )}

        {messages && messages.length === 0 && !send.isPending && (
          <div className="small">
            <div className="text-secondary mb-2">Try one of these:</div>
            <div className="d-flex flex-column align-items-start gap-1">
              {SUGGESTIONS.map((suggestion) => (
                <Button
                  key={suggestion}
                  variant="link"
                  size="sm"
                  className="p-0 text-start"
                  onClick={() => void submit(suggestion)}
                >
                  {suggestion}
                </Button>
              ))}
            </div>
          </div>
        )}

        <div className="d-flex flex-column gap-3">
          {messages?.map((message, index) => (
            <Bubble key={`${index}-${message.role}`} message={message} />
          ))}
          {send.isPending && (
            <div className="align-self-start bg-body-secondary rounded px-3 py-2 small text-secondary">
              <Spinner animation="border" size="sm" className="me-2" />
              Looking it up…
            </div>
          )}
        </div>
        <div ref={endRef} />
      </Card.Body>

      <Card.Footer>
        {turn && !turn.persistent && (
          // Surfaced rather than hidden: an assistant that will forget this
          // conversation on restart is something the user should know.
          <Alert variant="warning" className="py-1 px-2 small mb-2">
            Conversation storage is unavailable, so this thread will not be remembered after a
            restart.
          </Alert>
        )}

        <Form
          onSubmit={(event) => {
            event.preventDefault()
            void submit(draft)
          }}
        >
          <div className="d-flex gap-2">
            <Form.Control
              size="sm"
              placeholder="Ask about tickets…"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              disabled={send.isPending}
            />
            <Button type="submit" size="sm" disabled={send.isPending || !draft.trim()}>
              Send
            </Button>
          </div>
        </Form>

        {send.isError && (
          <div className="text-danger small mt-2">{(send.error as Error).message}</div>
        )}

        {turn && (
          <div className="d-flex align-items-center gap-2 mt-2 text-secondary" style={{ fontSize: '0.7rem' }}>
            {turn.model && <Badge bg="light" text="dark">{turn.model}</Badge>}
            <span>
              {turn.tool_calls} lookup{turn.tool_calls === 1 ? '' : 's'}
            </span>
          </div>
        )}
      </Card.Footer>
    </Card>
  )
}

function Bubble({ message }: { message: ChatMessage }) {
  const mine = message.role === 'user'
  return (
    <div
      className={`px-3 py-2 rounded ${mine ? 'align-self-end bg-primary text-white' : 'align-self-start bg-body-secondary'}`}
      style={{ maxWidth: '80%', whiteSpace: 'pre-wrap' }}
    >
      <div className="small">{message.content}</div>
    </div>
  )
}
