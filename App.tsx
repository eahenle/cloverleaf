import { useCallback, useEffect, useRef, useState } from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { api } from './api'
import { Assistant } from './components/Assistant'
import { Editor } from './components/Editor'
import { FileTree } from './components/FileTree'
import { PdfPreview } from './components/PdfPreview'
import { ProjectStatus } from './components/ProjectStatus'
import type { ChatMessage, CompileStatus, ProposedEdit, TreeNode } from './types'

const initialStatus: CompileStatus = { state: 'idle', diagnostics: [], log_tail: '', revision: 0 }

export default function App() {
  const [tree, setTree] = useState<TreeNode[]>([])
  const [path, setPath] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [selectedText, setSelectedText] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState<CompileStatus>(initialStatus)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [assistantBusy, setAssistantBusy] = useState(false)
  const [toast, setToast] = useState('')
  const contentRef = useRef(content)
  contentRef.current = content

  const showError = (reason: unknown) => {
    setToast(reason instanceof Error ? reason.message : String(reason))
    window.setTimeout(() => setToast(''), 4000)
  }
  const refreshTree = useCallback(async () => setTree(await api.tree()), [])

  const openFile = useCallback(async (nextPath: string) => {
    try {
      const file = await api.read(nextPath)
      setPath(file.path); setContent(file.content); setDirty(false); setSelectedText('')
    } catch (reason) { showError(reason) }
  }, [])

  useEffect(() => {
    void refreshTree().then(() => openFile('main.tex')).catch(showError)
    void api.compile().catch(showError)
  }, [openFile, refreshTree])

  useEffect(() => {
    if (status.state !== 'compiling' && status.state !== 'idle') return
    const timer = window.setInterval(() => { void api.compileStatus().then(setStatus).catch(showError) }, 500)
    return () => window.clearInterval(timer)
  }, [status.state])

  const save = useCallback(async (compile = false) => {
    if (!path) return
    setSaving(true)
    try {
      await api.write(path, contentRef.current)
      setDirty(false)
      if (compile) setStatus(await api.compile())
    } catch (reason) { showError(reason) } finally { setSaving(false) }
  }, [path])

  useEffect(() => {
    if (!dirty || !path) return
    const saveTimer = window.setTimeout(() => void save(false), 700)
    const compileTimer = window.setTimeout(async () => { await save(false); setStatus(await api.compile()) }, 1600)
    return () => { window.clearTimeout(saveTimer); window.clearTimeout(compileTimer) }
  }, [content, dirty, path, save])

  const mutateTree = async (action: () => Promise<unknown>) => {
    try { await action(); await refreshTree() } catch (reason) { showError(reason) }
  }
  const create = (type: 'file' | 'directory') => {
    const value = window.prompt(type === 'file' ? 'New file path' : 'New folder path')
    if (value) void mutateTree(async () => { await api.create(value, type); if (type === 'file') await openFile(value) })
  }
  const rename = (oldPath: string) => {
    const value = window.prompt('Rename to', oldPath)
    if (value && value !== oldPath) void mutateTree(async () => {
      await api.rename(oldPath, value)
      if (path === oldPath) setPath(value)
    })
  }
  const remove = (target: string) => {
    if (!window.confirm(`Delete ${target}?`)) return
    void mutateTree(async () => { await api.delete(target); if (path === target) { setPath(null); setContent('') } })
  }

  const sendMessage = async (message: string) => {
    const next: ChatMessage[] = [...messages, { role: 'user', content: message }]
    setMessages(next); setAssistantBusy(true)
    try {
      const result = await api.chat(next, {
        project_tree: tree, open_file: path, open_file_content: content,
        selected_text: selectedText || null, diagnostics: status.diagnostics,
      })
      setMessages([...next, { role: 'assistant', content: result.message, edits: result.proposed_edits }])
    } catch (reason) { showError(reason) } finally { setAssistantBusy(false) }
  }
  const applyEdit = async (edit: ProposedEdit) => {
    if (!window.confirm(`Apply Codex's proposed replacement to ${edit.path}?`)) return
    try {
      await api.write(edit.path, edit.content); await refreshTree()
      if (path === edit.path) { setContent(edit.content); setDirty(false) } else await openFile(edit.path)
      setStatus(await api.compile())
    } catch (reason) { showError(reason) }
  }

  return <main className="app-shell">
    <div className="topbar"><span className="wordmark">cloverleaf</span><span className="project-path">workspace / {path ?? '—'}</span></div>
    <PanelGroup direction="horizontal" className="workbench">
      <Panel defaultSize={18} minSize={12} maxSize={30}>
        <PanelGroup direction="vertical">
          <Panel defaultSize={68} minSize={30}><FileTree tree={tree} activePath={path} onOpen={openFile} onCreate={create} onRename={rename} onDelete={remove} /></Panel>
          <PanelResizeHandle className="resize-handle horizontal" />
          <Panel defaultSize={32} minSize={18}><ProjectStatus status={status} onCompile={() => void save(true)} /></Panel>
        </PanelGroup>
      </Panel>
      <PanelResizeHandle className="resize-handle" />
      <Panel defaultSize={49} minSize={28}>
        <Editor path={path} value={content} saveState={saving ? 'saving…' : dirty ? 'modified' : 'saved'} onChange={value => { setContent(value); setDirty(true) }} onSelection={setSelectedText} onSaveAndCompile={() => void save(true)} />
      </Panel>
      <PanelResizeHandle className="resize-handle" />
      <Panel defaultSize={33} minSize={22}>
        <PanelGroup direction="vertical">
          <Panel defaultSize={59} minSize={25}><PdfPreview status={status} /></Panel>
          <PanelResizeHandle className="resize-handle horizontal" />
          <Panel defaultSize={41} minSize={22}><Assistant messages={messages} busy={assistantBusy} onSend={sendMessage} onApplyEdit={applyEdit} /></Panel>
        </PanelGroup>
      </Panel>
    </PanelGroup>
    {toast && <div className="toast">{toast}</div>}
  </main>
}
