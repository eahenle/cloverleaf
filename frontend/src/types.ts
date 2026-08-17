export type TreeNode = {
  name: string
  path: string
  type: 'file' | 'directory'
  children: TreeNode[]
}

export type FileContent = {
  path: string
  content: string
  version?: string | null
}

export type ProjectInfo = {
  workspace: string
  name: string
  main_file: string
}

export type DirectoryListing = {
  path: string
  parent: string | null
  home: string
  root: string
  directories: Array<{ name: string; path: string }>
  tex_files: string[]
}

export type Diagnostic = {
  severity: 'error' | 'warning'
  message: string
  file?: string | null
  line?: number | null
}

export type CompileStatus = {
  state: 'idle' | 'compiling' | 'success' | 'error'
  diagnostics: Diagnostic[]
  log_tail: string
  revision: number
}

export type ProposedEdit = {
  path: string
  content: string
  summary: string
  replacements?: Array<{ old_text: string; new_text: string }>
  version: string | null
  is_new: boolean
}

export type ChatMessage = {
  role: 'user' | 'assistant'
  content: string
  edits?: ProposedEdit[]
}

export type AssistantContext = {
  main_file: string | null
  open_file: string | null
  open_file_content: string
  selected_text: string | null
  diagnostics: Diagnostic[]
  compile_state: CompileStatus['state']
  compile_log: string
}

export type AssistantResponse = {
  message: string
  proposed_edits: ProposedEdit[]
}

export type AssistantProgress = {
  phase: string
  message: string
  activity_count: number
  heartbeat: boolean
  received_at: number
  started_at: number
}

export type RuntimeInfo = {
  managed: boolean
  log_available: boolean
  shutdown_available: boolean
}
