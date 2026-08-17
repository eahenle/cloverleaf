import type {
  AssistantContext,
  AssistantProgress,
  AssistantResponse,
  ChatMessage,
  CompileStatus,
  DirectoryListing,
  FileContent,
  ProjectInfo,
  RuntimeInfo,
  TreeNode,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { cache: 'no-store', ...init })
  if (!response.ok) {
    throw new Error(await responseError(response))
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

async function responseError(response: Response): Promise<string> {
  let detail = `${response.status} ${response.statusText}`
  try {
    const payload = (await response.json()) as { detail?: string }
    if (payload.detail) detail = payload.detail
  } catch {
    // Keep the HTTP status when the server did not return JSON.
  }
  return detail
}

async function progressChat(
  messages: ChatMessage[],
  context: AssistantContext,
  startedAt: number,
  onProgress: (progress: AssistantProgress) => void,
): Promise<AssistantResponse> {
  const requestId = crypto.randomUUID()
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const socket = new WebSocket(
    `${protocol}//${window.location.host}/api/assistant/progress/${requestId}`,
  )
  const connected = await new Promise<boolean>((resolve) => {
    let settled = false
    const finish = (value: boolean) => {
      if (settled) return
      settled = true
      window.clearTimeout(timeout)
      resolve(value)
    }
    const timeout = window.setTimeout(() => finish(false), 2_000)
    socket.onopen = () => finish(true)
    socket.onerror = () => finish(false)
    socket.onmessage = (event) => {
      const progress = JSON.parse(String(event.data)) as Omit<
        AssistantProgress,
        'received_at' | 'started_at'
      >
      onProgress({ ...progress, started_at: startedAt, received_at: Date.now() })
    }
  })
  if (!connected) {
    socket.close()
    onProgress({
      phase: 'connection',
      message: 'The progress channel is unavailable; the Codex request is still running…',
      activity_count: 0,
      heartbeat: false,
      received_at: Date.now(),
      started_at: startedAt,
    })
  }

  try {
    return await request<AssistantResponse>('/api/assistant/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: messages.map(({ role, content }) => ({ role, content })),
        context,
        request_id: requestId,
      }),
    })
  } finally {
    socket.close(1000)
  }
}

function encodePath(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/')
}

export const api = {
  health: () => request<{ ok: boolean }>('/api/health'),
  runtime: () => request<RuntimeInfo>('/api/runtime'),
  shutdownRuntime: () =>
    request<{ ok: boolean; message: string }>('/api/runtime/shutdown', { method: 'POST' }),
  watchRuntimeLogs: (onChunk: (chunk: string) => void, onError: () => void) => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(`${protocol}//${window.location.host}/api/runtime/logs`)
    socket.onmessage = (event) => onChunk(String(event.data))
    socket.onerror = onError
    return () => socket.close(1000)
  },
  project: () => request<ProjectInfo>('/api/project'),
  browseDirectories: (path?: string) => {
    const query = path ? `?path=${encodeURIComponent(path)}` : ''
    return request<DirectoryListing>(`/api/project/directories${query}`)
  },
  loadProject: (workspace: string, mainFile: string) =>
    request<ProjectInfo>('/api/project/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workspace, main_file: mainFile }),
    }),
  tree: () => request<TreeNode[]>('/api/project/tree'),
  read: (path: string) => request<FileContent>(`/api/files/${encodePath(path)}`),
  watch: (
    path: string,
    onChange: (file: FileContent) => void,
    onDelete: () => void,
    onError: () => void,
  ) => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${window.location.host}/api/file-events/${encodePath(path)}`
    let socket: WebSocket | null = null
    let reconnectTimer: number | null = null
    let stopped = false

    const connect = () => {
      if (stopped) return
      const nextSocket = new WebSocket(url)
      socket = nextSocket
      nextSocket.onmessage = (event) => {
        const payload = JSON.parse(String(event.data)) as FileContent & { deleted?: boolean }
        if (payload.deleted) {
          stopped = true
          onDelete()
        } else {
          onChange(payload)
        }
      }
      nextSocket.onerror = onError
      nextSocket.onclose = () => {
        if (!stopped) reconnectTimer = window.setTimeout(connect, 1_000)
      }
    }
    connect()

    return () => {
      stopped = true
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      socket?.close(1000)
    }
  },
  write: (path: string, content: string, version?: string | null) =>
    request<FileContent>(`/api/files/${encodePath(path)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, content, version }),
    }),
  create: (path: string, type: 'file' | 'directory', content = '') =>
    request<{ ok: boolean }>('/api/files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, type, content }),
    }),
  rename: (path: string, newPath: string) =>
    request<{ ok: boolean }>('/api/files', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, new_path: newPath }),
    }),
  delete: (path: string) =>
    request<void>(`/api/files/${encodePath(path)}`, { method: 'DELETE' }),
  applyEdits: (edits: Array<{
    path: string
    content: string
    version: string | null
    is_new: boolean
  }>) =>
    request<{ files: FileContent[] }>('/api/files/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edits }),
    }),
  compile: () => request<CompileStatus>('/api/compile', { method: 'POST' }),
  compileStatus: () => request<CompileStatus>('/api/compile/status'),
  chat: (
    messages: ChatMessage[],
    context: AssistantContext,
    startedAt: number,
    onProgress: (progress: AssistantProgress) => void,
  ) => progressChat(messages, context, startedAt, onProgress),
}
