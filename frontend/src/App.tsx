import { useCallback, useEffect, useRef, useState } from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { api } from './api'
import { Assistant } from './components/Assistant'
import { Editor } from './components/Editor'
import { FileTree } from './components/FileTree'
import { PdfPreview } from './components/PdfPreview'
import { ProjectPicker } from './components/ProjectPicker'
import { ProjectStatus } from './components/ProjectStatus'
import type { ChatMessage, CompileStatus, ProjectInfo, ProposedEdit, TreeNode } from './types'

const initialStatus: CompileStatus = {
  state: 'idle',
  diagnostics: [],
  log_tail: '',
  revision: 0,
}

type EditIntent = { id: number; path: string }

export default function App() {
  const [project, setProject] = useState<ProjectInfo | null>(null)
  const [tree, setTree] = useState<TreeNode[]>([])
  const [path, setPath] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [selectedText, setSelectedText] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [editIntent, setEditIntent] = useState<EditIntent | null>(null)
  const [status, setStatus] = useState<CompileStatus>(initialStatus)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [assistantBusy, setAssistantBusy] = useState(false)
  const [projectLoading, setProjectLoading] = useState(false)
  const [projectPickerOpen, setProjectPickerOpen] = useState(false)
  const [toast, setToast] = useState('')

  const pathRef = useRef(path)
  const contentRef = useRef(content)
  const dirtyRef = useRef(dirty)
  const fileVersionRef = useRef<string | null>(null)
  const syncedContentRef = useRef('')
  const externalWarningVersionRef = useRef<string | null>(null)
  const editVersionRef = useRef(0)
  const saveCountRef = useRef(0)
  const toastTimerRef = useRef<number | null>(null)
  const initializedRef = useRef(false)
  const switchingProjectRef = useRef(false)

  const updatePath = useCallback((nextPath: string | null) => {
    pathRef.current = nextPath
    setPath(nextPath)
  }, [])

  const updateContent = useCallback((nextContent: string) => {
    contentRef.current = nextContent
    setContent(nextContent)
  }, [])

  const updateDirty = useCallback((nextDirty: boolean) => {
    dirtyRef.current = nextDirty
    setDirty(nextDirty)
  }, [])

  const showError = useCallback((reason: unknown) => {
    const message = reason instanceof Error ? reason.message : String(reason)
    setToast(message)
    if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current)
    toastTimerRef.current = window.setTimeout(() => setToast(''), 5000)
  }, [])

  const refreshTree = useCallback(async () => {
    setTree(await api.tree())
  }, [])

  const saveCurrent = useCallback(async (requestCompile: boolean) => {
    const targetPath = pathRef.current
    if (!targetPath) {
      if (requestCompile) setStatus(await api.compile())
      return
    }
    const snapshot = contentRef.current
    const version = editVersionRef.current
    saveCountRef.current += 1
    setSaving(true)
    try {
      const saved = await api.write(targetPath, snapshot, fileVersionRef.current)
      if (pathRef.current === targetPath) {
        fileVersionRef.current = saved.version ?? null
        syncedContentRef.current = snapshot
        externalWarningVersionRef.current = null
        if (editVersionRef.current === version) updateDirty(false)
      }
      if (requestCompile) setStatus(await api.compile())
    } finally {
      saveCountRef.current -= 1
      if (saveCountRef.current === 0) setSaving(false)
    }
  }, [updateDirty])

  const openFile = useCallback(async (nextPath: string) => {
    if (pathRef.current === nextPath) return
    if (dirtyRef.current) await saveCurrent(true)
    setEditIntent(null)
    const file = await api.read(nextPath)
    fileVersionRef.current = file.version ?? null
    syncedContentRef.current = file.content
    externalWarningVersionRef.current = null
    updatePath(file.path)
    updateContent(file.content)
    updateDirty(false)
    setSelectedText('')
  }, [saveCurrent, updateContent, updateDirty, updatePath])

  const requestCompile = useCallback(async () => {
    setStatus(await api.compile())
  }, [])

  const saveAndCompile = useCallback(() => {
    setEditIntent(null)
    void saveCurrent(true).catch(showError)
  }, [saveCurrent, showError])

  const editorChanged = useCallback((value: string) => {
    if (!pathRef.current) return
    updateContent(value)
    updateDirty(true)
    editVersionRef.current += 1
    setEditIntent({ id: editVersionRef.current, path: pathRef.current })
  }, [updateContent, updateDirty])

  useEffect(() => {
    if (!editIntent) return
    const saveTimer = window.setTimeout(() => {
      if (!switchingProjectRef.current && pathRef.current === editIntent.path) {
        void saveCurrent(false).catch(showError)
      }
    }, 700)
    const compileTimer = window.setTimeout(() => {
      if (switchingProjectRef.current || pathRef.current !== editIntent.path) return
      void saveCurrent(true)
        .then(() => {
          setEditIntent((current) => (current?.id === editIntent.id ? null : current))
        })
        .catch(showError)
    }, 1600)
    return () => {
      window.clearTimeout(saveTimer)
      window.clearTimeout(compileTimer)
    }
  }, [editIntent, saveCurrent, showError])

  useEffect(() => {
    if (status.state !== 'compiling') return
    let active = true
    const poll = () => {
      void api.compileStatus()
        .then((next) => {
          if (active) setStatus(next)
        })
        .catch(showError)
    }
    poll()
    const timer = window.setInterval(poll, 350)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [showError, status.state])

  useEffect(() => {
    if (!path) return
    let active = true
    const observedPath = path
    const stop = api.watch(
      observedPath,
      (file) => {
        if (!active || switchingProjectRef.current || pathRef.current !== observedPath) return
        const nextVersion = file.version ?? null
        const unchanged = nextVersion !== null && nextVersion === fileVersionRef.current
        if (unchanged || (nextVersion === null && file.content === syncedContentRef.current)) return
        if (dirtyRef.current || saveCountRef.current > 0) {
          setEditIntent(null)
          if (externalWarningVersionRef.current !== nextVersion) {
            externalWarningVersionRef.current = nextVersion
            showError(`${observedPath} changed on disk. Autosave is paused; your editor content was kept.`)
          }
          return
        }
        fileVersionRef.current = nextVersion
        syncedContentRef.current = file.content
        externalWarningVersionRef.current = null
        updateContent(file.content)
        setSelectedText('')
        if (/\.(tex|bib|sty|cls)$/i.test(observedPath)) {
          void requestCompile().catch(showError)
        }
      },
      () => {
        if (!active || pathRef.current !== observedPath) return
        setEditIntent(null)
        updateDirty(true)
        showError(`${observedPath} was removed on disk. Cloverleaf kept the editor content.`)
      },
      () => undefined,
    )
    return () => {
      active = false
      stop()
    }
  }, [path, project?.workspace, requestCompile, showError, updateContent, updateDirty])

  useEffect(() => {
    if (initializedRef.current) return
    initializedRef.current = true
    void (async () => {
      await api.health()
      const currentProject = await api.project()
      setProject(currentProject)
      await refreshTree()
      await openFile(currentProject.main_file)
      await requestCompile()
    })().catch(showError)
  }, [openFile, refreshTree, requestCompile, showError])

  useEffect(
    () => () => {
      if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current)
    },
    [],
  )

  const createEntry = (type: 'file' | 'directory') => {
    const value = window.prompt(type === 'file' ? 'New file path' : 'New folder path')
    const nextPath = value?.trim()
    if (!nextPath) return
    void (async () => {
      await api.create(nextPath, type)
      await refreshTree()
      if (type === 'file') await openFile(nextPath)
    })().catch(showError)
  }

  const loadProject = async (workspace: string, mainFile: string) => {
    switchingProjectRef.current = true
    setProjectLoading(true)
    try {
      if (pathRef.current) await saveCurrent(false)
      setEditIntent(null)
      const loaded = await api.loadProject(workspace, mainFile)
      const nextTree = await api.tree()
      const file = await api.read(loaded.main_file)
      fileVersionRef.current = file.version ?? null
      syncedContentRef.current = file.content
      externalWarningVersionRef.current = null
      setProject(loaded)
      setTree(nextTree)
      updatePath(file.path)
      updateContent(file.content)
      updateDirty(false)
      setSelectedText('')
      setMessages([])
      setStatus(initialStatus)
      await requestCompile()
    } finally {
      switchingProjectRef.current = false
      setProjectLoading(false)
    }
  }

  const renameEntry = (oldPath: string) => {
    const value = window.prompt('Rename to', oldPath)
    const newPath = value?.trim()
    if (!newPath || newPath === oldPath) return
    void (async () => {
      if (dirtyRef.current) await saveCurrent(false)
      setEditIntent(null)
      await api.rename(oldPath, newPath)
      const activePath = pathRef.current
      if (activePath === oldPath || activePath?.startsWith(`${oldPath}/`)) {
        updatePath(`${newPath}${activePath.slice(oldPath.length)}`)
        updateDirty(false)
      }
      await refreshTree()
    })().catch(showError)
  }

  const deleteEntry = (target: string) => {
    if (!window.confirm(`Delete ${target}?`)) return
    void (async () => {
      await api.delete(target)
      const activePath = pathRef.current
      if (activePath === target || activePath?.startsWith(`${target}/`)) {
        setEditIntent(null)
        updatePath(null)
        fileVersionRef.current = null
        syncedContentRef.current = ''
        externalWarningVersionRef.current = null
        updateContent('')
        updateDirty(false)
        setSelectedText('')
      }
      await refreshTree()
    })().catch(showError)
  }

  const sendMessage = async (message: string) => {
    const next: ChatMessage[] = [...messages, { role: 'user', content: message }]
    setMessages(next)
    setAssistantBusy(true)
    try {
      const result = await api.chat(next, {
        open_file: pathRef.current,
        open_file_content: contentRef.current,
        selected_text: selectedText || null,
        diagnostics: status.diagnostics,
      })
      setMessages([...next, { role: 'assistant', content: result.message, edits: result.proposed_edits }])
    } catch (reason) {
      showError(reason)
      setMessages([...next, {
        role: 'assistant',
        content: reason instanceof Error ? reason.message : 'The assistant could not answer.',
      }])
    } finally {
      setAssistantBusy(false)
    }
  }

  const applyEdit = async (edit: ProposedEdit): Promise<boolean> => {
    if (!window.confirm(`Apply Codex's proposed replacement to ${edit.path}?`)) return false
    try {
      if (dirtyRef.current && pathRef.current !== edit.path) await saveCurrent(false)
      setEditIntent(null)
      const saved = await api.write(
        edit.path,
        edit.content,
        pathRef.current === edit.path ? fileVersionRef.current : null,
      )
      await refreshTree()
      updatePath(edit.path)
      fileVersionRef.current = saved.version ?? null
      syncedContentRef.current = edit.content
      externalWarningVersionRef.current = null
      updateContent(edit.content)
      updateDirty(false)
      setSelectedText('')
      await requestCompile()
      return true
    } catch (reason) {
      showError(reason)
      return false
    }
  }

  return (
    <main className="app-shell">
      <div className="topbar">
        <span className="wordmark">cloverleaf</span>
        <span className="project-path" title={project?.workspace}>
          {project?.name ?? 'workspace'} / {path ?? '—'}
        </span>
        <span className={`topbar-build ${status.state}`}>
          <span className={`status-dot ${status.state}`} /> {status.state}
        </span>
      </div>
      <PanelGroup direction="horizontal" className="workbench" autoSaveId="cloverleaf-main">
        <Panel defaultSize={18} minSize={13} maxSize={30}>
          <PanelGroup direction="vertical" autoSaveId="cloverleaf-left">
            <Panel defaultSize={67} minSize={32}>
              <FileTree
                key={project?.workspace ?? 'no-project'}
                tree={tree}
                activePath={path}
                onOpen={(nextPath) => void openFile(nextPath).catch(showError)}
                onLoadProject={() => setProjectPickerOpen(true)}
                loadDisabled={!project || assistantBusy || projectLoading}
                onCreate={createEntry}
                onRename={renameEntry}
                onDelete={deleteEntry}
              />
            </Panel>
            <PanelResizeHandle className="resize-handle horizontal" aria-label="Resize project and compiler panels" />
            <Panel defaultSize={33} minSize={20}>
              <ProjectStatus status={status} onCompile={saveAndCompile} />
            </Panel>
          </PanelGroup>
        </Panel>
        <PanelResizeHandle className="resize-handle" aria-label="Resize project and editor panels" />
        <Panel defaultSize={49} minSize={28}>
          <Editor
            path={path}
            value={content}
            saveState={saving ? 'saving…' : dirty ? 'modified' : path ? 'saved' : '—'}
            onChange={editorChanged}
            onSelection={setSelectedText}
            onSaveAndCompile={saveAndCompile}
          />
        </Panel>
        <PanelResizeHandle className="resize-handle" aria-label="Resize editor and preview panels" />
        <Panel defaultSize={33} minSize={23}>
          <PanelGroup direction="vertical" autoSaveId="cloverleaf-right">
            <Panel defaultSize={59} minSize={28}>
              <PdfPreview status={status} projectKey={project?.workspace ?? null} />
            </Panel>
            <PanelResizeHandle className="resize-handle horizontal" aria-label="Resize preview and assistant panels" />
            <Panel defaultSize={41} minSize={24}>
              <Assistant
                messages={messages}
                busy={assistantBusy}
                onSend={(message) => void sendMessage(message)}
                onApplyEdit={applyEdit}
              />
            </Panel>
          </PanelGroup>
        </Panel>
      </PanelGroup>
      {projectPickerOpen && project && (
        <ProjectPicker
          currentProject={project}
          onClose={() => setProjectPickerOpen(false)}
          onLoad={loadProject}
        />
      )}
      {toast && <div className="toast" role="alert">{toast}</div>}
    </main>
  )
}
