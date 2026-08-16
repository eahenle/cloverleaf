import type {
  AssistantContext,
  AssistantResponse,
  ChatMessage,
  CompileStatus,
  DirectoryListing,
  FileContent,
  ProjectInfo,
  TreeNode,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { cache: 'no-store', ...init })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload.detail) detail = payload.detail
    } catch {
      // Keep the HTTP status when the server did not return JSON.
    }
    throw new Error(detail)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

function encodePath(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/')
}

export const api = {
  health: () => request<{ ok: boolean }>('/api/health'),
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
  create: (path: string, type: 'file' | 'directory') =>
    request<{ ok: boolean }>('/api/files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, type }),
    }),
  rename: (path: string, newPath: string) =>
    request<{ ok: boolean }>('/api/files', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, new_path: newPath }),
    }),
  delete: (path: string) =>
    request<void>(`/api/files/${encodePath(path)}`, { method: 'DELETE' }),
  compile: () => request<CompileStatus>('/api/compile', { method: 'POST' }),
  compileStatus: () => request<CompileStatus>('/api/compile/status'),
  chat: (messages: ChatMessage[], context: AssistantContext) =>
    request<AssistantResponse>('/api/assistant/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: messages.map(({ role, content }) => ({ role, content })),
        context,
      }),
    }),
}
