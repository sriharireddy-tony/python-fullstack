/**
 * Status and priority badges.
 *
 * Colour is what makes a forty-row table scannable, so the mapping is defined
 * once here rather than inline at each call site. Every badge also carries its
 * meaning in text — colour alone is not an accessible signal.
 */

import { Badge } from 'react-bootstrap'
import type { Priority, TicketStatus } from '@/api/types'

const PRIORITY_STYLE: Record<Priority, { bg: string; label: string }> = {
  p1: { bg: 'danger', label: 'P1' },
  p2: { bg: 'warning', label: 'P2' },
  p3: { bg: 'info', label: 'P3' },
  p4: { bg: 'secondary', label: 'P4' },
}

const STATUS_STYLE: Record<TicketStatus, { bg: string; label: string }> = {
  open: { bg: 'primary', label: 'Open' },
  assigned: { bg: 'info', label: 'Assigned' },
  in_progress: { bg: 'warning', label: 'In Progress' },
  on_hold: { bg: 'secondary', label: 'On Hold' },
  resolved: { bg: 'success', label: 'Resolved' },
  rejected: { bg: 'dark', label: 'Rejected' },
  closed: { bg: 'secondary', label: 'Closed' },
}

export function PriorityBadge({ priority, overridden }: { priority: Priority; overridden?: boolean }) {
  const style = PRIORITY_STYLE[priority]
  return (
    <Badge bg={style.bg} title={overridden ? 'Priority was manually overridden' : undefined}>
      {style.label}
      {overridden && ' *'}
    </Badge>
  )
}

export function StatusBadge({ status }: { status: TicketStatus }) {
  const style = STATUS_STYLE[status]
  return <Badge bg={style.bg}>{style.label}</Badge>
}

export function statusLabel(status: TicketStatus): string {
  return STATUS_STYLE[status].label
}

/** Relative age, with the absolute local time on hover. */
export function RelativeTime({ value }: { value: string }) {
  const date = new Date(value)
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000)

  let text: string
  if (seconds < 60) text = 'just now'
  else if (seconds < 3600) text = `${Math.floor(seconds / 60)}m ago`
  else if (seconds < 86400) text = `${Math.floor(seconds / 3600)}h ago`
  else if (seconds < 2592000) text = `${Math.floor(seconds / 86400)}d ago`
  else text = date.toLocaleDateString()

  return (
    <span title={date.toLocaleString()} className="text-nowrap">
      {text}
    </span>
  )
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
