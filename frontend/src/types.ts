export type TreeNode = {
  name: string
  path: string
  type: 'file' | 'directory'
  children: TreeNode[]
}

export type FileContent = {
  path: string
  content: string
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
}

export type ChatMessage = {
  role: 'user' | 'assistant'
  content: string
  edits?: ProposedEdit[]
}

export type AssistantContext = {
  project_tree: TreeNode[]
  open_file: string | null
  open_file_content: string
  selected_text: string | null
  diagnostics: Diagnostic[]
}

export type AssistantResponse = {
  message: string
  proposed_edits: ProposedEdit[]
}
