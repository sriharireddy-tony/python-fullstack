/**
 * HTTP client.
 *
 * Three things it guarantees so no call site has to remember them:
 *
 * 1. `credentials: 'include'` on every request, because authentication rides on
 *    httpOnly cookies rather than a token the JavaScript can read.
 * 2. The CSRF token header on every state-changing method.
 * 3. Errors arrive as a typed `ApiError` carrying the machine-readable `code`,
 *    so UI logic switches on the code and never on the human message.
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? ''
const API_PREFIX = `${BASE_URL}/api/v1`

const CSRF_COOKIE = 'csrf_token'
const CSRF_HEADER = 'X-CSRF-Token'

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/** RFC 9457 Problem Details, as returned by the backend. */
export interface ProblemDetails {
  type: string
  title: string
  status: number
  detail: string
  code: string
  request_id?: string
  errors?: { field: string; message: string }[]
}

export class ApiError extends Error {
  readonly status: number
  /** Stable machine-readable identifier. Switch on this, never on `message`. */
  readonly code: string
  readonly requestId?: string
  readonly fieldErrors: { field: string; message: string }[]

  constructor(problem: ProblemDetails) {
    super(problem.detail || problem.title)
    this.name = 'ApiError'
    this.status = problem.status
    this.code = problem.code
    this.requestId = problem.request_id
    this.fieldErrors = problem.errors ?? []
  }

  get isAuthError() {
    return this.status === 401
  }
  get isPermissionError() {
    return this.status === 403
  }
  get isConflict() {
    return this.status === 409
  }
  get isVersionConflict() {
    return this.code === 'TICKET_VERSION_CONFLICT'
  }
}

/** Network failure, timeout, or a response that was not valid Problem Details. */
export class NetworkError extends Error {
  constructor(message = 'Unable to reach the server. Check your connection.') {
    super(message)
    this.name = 'NetworkError'
  }
}

function readCookie(name: string): string | undefined {
  return document.cookie
    .split('; ')
    .find((row) => row.startsWith(`${name}=`))
    ?.split('=')[1]
}

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  /** Appended as a query string; undefined and null values are dropped. */
  query?: Record<string, string | number | boolean | undefined | null | string[]>
  /** Sent as Idempotency-Key. Use on creates that a double-click could duplicate. */
  idempotencyKey?: string
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = `${API_PREFIX}${path}`
  if (!query) return url

  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      // Repeated params mean OR within a field (see docs/06-api.md)
      value.forEach((v) => params.append(key, String(v)))
    } else {
      params.append(key, String(value))
    }
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, query, idempotencyKey, headers, ...rest } = options
  const method = (rest.method ?? 'GET').toUpperCase()

  const finalHeaders = new Headers(headers)
  if (body !== undefined) finalHeaders.set('Content-Type', 'application/json')
  if (idempotencyKey) finalHeaders.set('Idempotency-Key', idempotencyKey)

  if (UNSAFE_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE)
    if (csrf) finalHeaders.set(CSRF_HEADER, csrf)
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, query), {
      ...rest,
      method,
      headers: finalHeaders,
      credentials: 'include',
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new NetworkError()
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  const payload: unknown = text ? safeJsonParse(text) : undefined

  if (!response.ok) {
    if (isProblemDetails(payload)) throw new ApiError(payload)
    throw new ApiError({
      type: 'about:blank',
      title: response.statusText || 'Request failed',
      status: response.status,
      detail: response.statusText || 'Request failed',
      code: 'UNKNOWN_ERROR',
    })
  }

  return payload as T
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

function isProblemDetails(value: unknown): value is ProblemDetails {
  return (
    typeof value === 'object' &&
    value !== null &&
    'code' in value &&
    'status' in value &&
    'title' in value
  )
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'POST', body }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'PATCH', body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'DELETE' }),
}

/** Envelope returned by every list endpoint (docs/06-api.md). */
export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}
