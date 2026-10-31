import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { api } from './api'
import { Assistant } from './components/Assistant'
import { Editor } from './components/Editor'
import { FileTree } from './components/FileTree'
import { PdfPreview } from './components/PdfPreview'
import { ProjectPicker } from './components/ProjectPicker'
import { ProjectStatus } from './components/ProjectStatus'
import type { AssistantProgress, ChatMessage, CompileStatus, ProjectInfo, ProposedEdit, SelectedLine, SourceLocation, PdfSourcePoint, TreeNode } from './types'

const initialStatus: CompileStatus = {
  state: 'idle',
  diagnostics: [],
  log_tail: '',
  revision: 0,
}

type EditIntent = { id: number; path: string }
type AssistantTurn = { message: string; attachedPaths: string[]; selectedLine: SelectedLine | null }

function treeFiles(nodes: TreeNode[]): string[] {
  return nodes.flatMap((node) => node.type === 'file' ? [node.path] : treeFiles(node.children))
}

function pendingReviewMessageIndex(
  messages: ChatMessage[],
  dismissedEditKeys: Set<string>,
): number | null {
  for (let messageIndex = 0; messageIndex < messages.length; messageIndex += 1) {
    const edits = messages[messageIndex].edits ?? []
    if (edits.some((_edit, editIndex) => !dismissedEditKeys.has(`${messageIndex}-${editIndex}`))) {
      return messageIndex
    }
  }
  return null
}

export default function App() {
  const [project, setProject] = useState<ProjectInfo | null>(null)
  const [tree, setTree] = useState<TreeNode[]>([])
  const [path, setPath] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [selectedText, setSelectedText] = useState('')
  const [selectedLine, setSelectedLine] = useState<SelectedLine | null>(null)
  const [sourceLocation, setSourceLocation] = useState<(SourceLocation & { id: number }) | null>(null)
  const sourceLookupId = useRef(0)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [editIntent, setEditIntent] = useState<EditIntent | null>(null)
  const [status, setStatus] = useState<CompileStatus>(initialStatus)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [assistantResetKey, setAssistantResetKey] = useState(0)
  const [assistantBusy, setAssistantBusy] = useState(false)
  const [assistantCancelling, setAssistantCancelling] = useState(false)
  const [assistantQueue, setAssistantQueue] = useState<AssistantTurn[]>([])
  const [assistantReviewMessageIndex, setAssistantReviewMessageIndex] = useState<number | null>(null)
  const [dismissedEditKeys, setDismissedEditKeys] = useState<Set<string>>(new Set())
  const [assistantProgress, setAssistantProgress] = useState<AssistantProgress | null>(null)
  const [projectLoading, setProjectLoading] = useState(false)
  const [compilationRootBusy, setCompilationRootBusy] = useState(false)
  const [projectPickerMode, setProjectPickerMode] = useState<'load' | 'new' | null>(null)
  const [toast, setToast] = useState('')
  const availableFiles = useMemo(() => treeFiles(tree), [tree])
  const queuedMessages = useMemo(() => assistantQueue.map((turn) => turn.message), [assistantQueue])

  const pathRef = useRef(path)
  const contentRef = useRef(content)
  const dirtyRef = useRef(dirty)
  const fileVersionRef = useRef<string | null>(null)
  const syncedContentRef = useRef('')
  const externalWarningVersionRef = useRef<string | null>(null)
  const editVersionRef = useRef(0)
  const saveCountRef = useRef(0)
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve())
  const messagesRef = useRef<ChatMessage[]>([])
  const assistantQueueRef = useRef<AssistantTurn[]>([])
  const assistantReviewMessageIndexRef = useRef<number | null>(null)
  const dismissedEditKeysRef = useRef<Set<string>>(new Set())
  const historyWorkspaceRef = useRef<string | null>(null)
  const historySaveQueueRef = useRef<Promise<void>>(Promise.resolve())
  const assistantRunningRef = useRef(false)
  const assistantCancelRequestedRef = useRef(false)
  const assistantCancelledQueueCountRef = useRef(0)
  const activeAssistantRequestRef = useRef<{
    requestId: string
    controller: AbortController
  } | null>(null)
  const assistantContextRef = useRef({ project, selectedText, status })
  const toastTimerRef = useRef<number | null>(null)
  const initializedRef = useRef(false)
  const switchingProjectRef = useRef(false)

  useEffect(() => {
    assistantContextRef.current = { project, selectedText, status }
  }, [project, selectedText, status])

  const replaceMessages = useCallback((nextMessages: ChatMessage[]) => {
    messagesRef.current = nextMessages
    setMessages(nextMessages)
  }, [])

  const replaceAssistantQueue = useCallback((nextQueue: AssistantTurn[]) => {
    assistantQueueRef.current = nextQueue
    setAssistantQueue(nextQueue)
  }, [])

  const replaceAssistantReview = useCallback((messageIndex: number | null) => {
    assistantReviewMessageIndexRef.current = messageIndex
    setAssistantReviewMessageIndex(messageIndex)
  }, [])

  const replaceDismissedEditKeys = useCallback((nextKeys: Set<string>) => {
    dismissedEditKeysRef.current = nextKeys
    setDismissedEditKeys(nextKeys)
  }, [])

  const restoreAssistantHistory = useCallback((history: {
    workspace: string
    messages: ChatMessage[]
    dismissed_edit_keys: string[]
  }) => {
    const restoredDismissed = new Set(history.dismissed_edit_keys)
    historyWorkspaceRef.current = history.workspace
    replaceMessages(history.messages)
    replaceDismissedEditKeys(restoredDismissed)
    replaceAssistantReview(pendingReviewMessageIndex(history.messages, restoredDismissed))
  }, [replaceAssistantReview, replaceDismissedEditKeys, replaceMessages])

  const updatePath = useCallback((nextPath: string | null) => {
    setSourceLocation(null)
    pathRef.current = nextPath
    setPath(nextPath)
  }, [])

  const updateContent = useCallback((nextContent: string) => {
    setSourceLocation(null)
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

  useEffect(() => {
    const workspace = project?.workspace
    if (!workspace || historyWorkspaceRef.current !== workspace) return
    const snapshot = {
      version: 1 as const,
      workspace,
      messages,
      dismissed_edit_keys: [...dismissedEditKeys],
    }
    const operation = historySaveQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        if (historyWorkspaceRef.current !== workspace) return
        await api.saveAssistantHistory(snapshot)
      })
    historySaveQueueRef.current = operation.catch(() => undefined)
    void operation.catch((reason) => {
      if (historyWorkspaceRef.current === workspace) showError(reason)
    })
  }, [dismissedEditKeys, messages, project?.workspace, showError])

  const refreshTree = useCallback(async () => {
    setTree(await api.tree())
  }, [])

  const saveCurrent = useCallback((requestCompile: boolean): Promise<void> => {
    const targetPath = pathRef.current
    if (!targetPath) {
      return requestCompile
        ? api.compile().then((nextStatus) => setStatus(nextStatus))
        : Promise.resolve()
    }
    const snapshot = contentRef.current
    const version = editVersionRef.current
    const scheduledFileVersion = fileVersionRef.current
    saveCountRef.current += 1
    setSaving(true)

    const operation = saveQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        try {
          const expectedVersion = pathRef.current === targetPath
            ? fileVersionRef.current
            : scheduledFileVersion
          const saved = await api.write(targetPath, snapshot, expectedVersion)
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
      })
    saveQueueRef.current = operation.catch(() => undefined)
    return operation
  }, [updateDirty])

  const openFile = useCallback(async (nextPath: string, isCurrent?: () => boolean) => {
    if (!isCurrent) sourceLookupId.current += 1
    if (pathRef.current === nextPath) return
    if (dirtyRef.current) await saveCurrent(true)
    if (isCurrent && !isCurrent()) return
    setEditIntent(null)
    const file = await api.read(nextPath)
    if (isCurrent && !isCurrent()) return
    fileVersionRef.current = file.version ?? null
    syncedContentRef.current = file.content
    externalWarningVersionRef.current = null
    updatePath(file.path)
    updateContent(file.content)
    updateDirty(false)
    setSelectedText('')
    setSelectedLine(null)
  }, [saveCurrent, updateContent, updateDirty, updatePath])

  const locatePdfSource = useCallback((point: PdfSourcePoint, displayedProjectKey: string) => {
    if (!project || switchingProjectRef.current) return
    const key = `${project.workspace}:${project.main_file}`
    if (displayedProjectKey !== key) return
    const id = ++sourceLookupId.current
    const isCurrent = () => {
      const active = assistantContextRef.current.project
      return id === sourceLookupId.current && !switchingProjectRef.current
        && active?.workspace === project.workspace && active.main_file === project.main_file
    }
    void (async () => {
      const location = await api.pdfSource(point, project.workspace, project.main_file)
      if (!isCurrent()) return
      await openFile(location.path, isCurrent)
      if (isCurrent()) setSourceLocation({ ...location, id })
    })().catch(reason => { if (isCurrent()) showError(reason) })
  }, [openFile, project, showError])

  const requestCompile = useCallback(async () => {
    setStatus(await api.compile())
  }, [])

  const saveAndCompile = useCallback(() => {
    setEditIntent(null)
    void saveCurrent(true).catch(showError)
  }, [saveCurrent, showError])

  const compileOpenView = useCallback(() => {
    const targetPath = pathRef.current
    if (!targetPath || !/\.tex$/i.test(targetPath)) return
    void (async () => {
      setCompilationRootBusy(true)
      try {
        setEditIntent(null)
        await saveCurrent(false)
        const updated = await api.selectCompilationRoot(targetPath)
        setProject(updated)
        setStatus(initialStatus)
        setStatus(await api.compile())
      } finally {
        setCompilationRootBusy(false)
      }
    })().catch(showError)
  }, [saveCurrent, showError])

  const editorChanged = useCallback((value: string) => {
    sourceLookupId.current += 1
    setSelectedLine(null)
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
        setSelectedLine(null)
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
      const history = await api.assistantHistory()
      setProject(currentProject)
      restoreAssistantHistory(history)
      await refreshTree()
      await openFile(currentProject.main_file)
      await requestCompile()
    })().catch(showError)
  }, [openFile, refreshTree, requestCompile, restoreAssistantHistory, showError])

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

  const switchProject = async (activate: () => Promise<ProjectInfo>) => {
    const previousHistoryWorkspace = historyWorkspaceRef.current
    historyWorkspaceRef.current = null
    switchingProjectRef.current = true
    setProjectLoading(true)
    try {
      if (pathRef.current) await saveCurrent(false)
      setEditIntent(null)
      const loaded = await activate()
      const history = await api.assistantHistory()
      const nextTree = await api.tree()
      const file = await api.read(loaded.main_file)
      fileVersionRef.current = file.version ?? null
      syncedContentRef.current = file.content
      externalWarningVersionRef.current = null
      setProject(loaded)
      setAssistantResetKey((current) => current + 1)
      setTree(nextTree)
      updatePath(file.path)
      updateContent(file.content)
      updateDirty(false)
      setSelectedText('')
      setSelectedLine(null)
      replaceAssistantQueue([])
      restoreAssistantHistory(history)
      setAssistantProgress(null)
      setStatus(initialStatus)
      await requestCompile()
    } finally {
      if (historyWorkspaceRef.current === null) {
        historyWorkspaceRef.current = previousHistoryWorkspace
      }
      switchingProjectRef.current = false
      setProjectLoading(false)
    }
  }

  const loadProject = (workspace: string, mainFile: string) =>
    switchProject(() => api.loadProject(workspace, mainFile))

  const createProject = (parent: string, name: string | null) =>
    switchProject(() => api.createProject(parent, name))

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
        setSelectedLine(null)
      }
      await refreshTree()
    })().catch(showError)
  }

  const runAssistantRounds = useCallback(async (firstTurn: AssistantTurn) => {
    assistantRunningRef.current = true
    assistantCancelRequestedRef.current = false
    assistantCancelledQueueCountRef.current = 0
    setAssistantBusy(true)
    let turn: AssistantTurn | undefined = firstTurn
    try {
      while (turn !== undefined) {
        const next: ChatMessage[] = [
          ...messagesRef.current,
          { role: 'user', content: turn.message },
        ]
        replaceMessages(next)
        const startedAt = Date.now()
        setAssistantProgress({
          phase: 'preparing',
          message: 'Preparing the open-file context…',
          activity_count: 0,
          heartbeat: false,
          received_at: startedAt,
          started_at: startedAt,
        })
        const requestId = crypto.randomUUID()
        const controller = new AbortController()
        activeAssistantRequestRef.current = { requestId, controller }
        try {
          const { project: activeProject, selectedText: activeSelection, status: activeStatus } =
            assistantContextRef.current
          const result = await api.chat(next, {
            main_file: activeProject?.main_file ?? null,
            open_file: pathRef.current,
            open_file_content: contentRef.current,
            selected_text: turn.selectedLine ? turn.selectedLine.text : activeSelection || null,
            selected_line: turn.selectedLine,
            diagnostics: activeStatus.diagnostics,
            compile_state: activeStatus.state,
            compile_log: activeStatus.log_tail,
          }, startedAt, setAssistantProgress, requestId, controller.signal, turn.attachedPaths)
          if (assistantCancelRequestedRef.current) {
            const cleared = assistantCancelledQueueCountRef.current
            replaceMessages([...next, {
              role: 'assistant',
              content: `Codex turn cancelled.${cleared ? ` ${cleared} queued round${cleared === 1 ? '' : 's'} cleared.` : ''}`,
            }])
          } else {
            const responseMessage: ChatMessage = {
              role: 'assistant',
              content: result.message,
              edits: result.proposed_edits,
              warnings: result.warnings,
            }
            replaceMessages([...next, responseMessage])
            if (result.proposed_edits.length > 0) {
              replaceAssistantReview(next.length)
            }
          }
        } catch (reason) {
          if (assistantCancelRequestedRef.current || (reason instanceof DOMException && reason.name === 'AbortError')) {
            const cleared = assistantCancelledQueueCountRef.current
            replaceMessages([...next, {
              role: 'assistant',
              content: `Codex turn cancelled.${cleared ? ` ${cleared} queued round${cleared === 1 ? '' : 's'} cleared.` : ''}`,
            }])
          } else {
            showError(reason)
            replaceMessages([...next, {
              role: 'assistant',
              content: reason instanceof Error ? reason.message : 'The assistant could not answer.',
            }])
          }
        } finally {
          if (activeAssistantRequestRef.current?.requestId === requestId) {
            activeAssistantRequestRef.current = null
          }
        }

        if (assistantCancelRequestedRef.current) break
        if (assistantReviewMessageIndexRef.current !== null) break
        const [queuedTurn, ...remainingQueue] = assistantQueueRef.current
        replaceAssistantQueue(remainingQueue)
        turn = queuedTurn
      }
    } finally {
      assistantRunningRef.current = false
      setAssistantBusy(false)
      setAssistantCancelling(false)
      setAssistantProgress(null)
      assistantCancelRequestedRef.current = false
      assistantCancelledQueueCountRef.current = 0
      activeAssistantRequestRef.current = null
    }
  }, [replaceAssistantQueue, replaceAssistantReview, replaceMessages, showError])

  const cancelAssistant = useCallback(() => {
    const active = activeAssistantRequestRef.current
    if (!active || assistantCancelRequestedRef.current) return
    assistantCancelRequestedRef.current = true
    assistantCancelledQueueCountRef.current = assistantQueueRef.current.length
    replaceAssistantQueue([])
    setAssistantCancelling(true)
    void api.cancelAssistant(active.requestId)
      .catch(() => undefined)
      .finally(() => active.controller.abort())
    window.setTimeout(() => active.controller.abort(), 750)
  }, [replaceAssistantQueue])

  const sendMessage = useCallback((message: string, attachedPaths: string[] = []) => {
    if (assistantCancelRequestedRef.current) return
    const turn = { message, attachedPaths: [...attachedPaths], selectedLine }
    if (assistantRunningRef.current || assistantReviewMessageIndexRef.current !== null) {
      replaceAssistantQueue([...assistantQueueRef.current, turn])
      return
    }
    void runAssistantRounds(turn)
  }, [replaceAssistantQueue, runAssistantRounds, selectedLine])

  const resolveAssistantEdits = useCallback((editKeys: string[]) => {
    const nextDismissed = new Set(dismissedEditKeysRef.current)
    editKeys.forEach((key) => nextDismissed.add(key))
    replaceDismissedEditKeys(nextDismissed)
    const pendingReview = pendingReviewMessageIndex(messagesRef.current, nextDismissed)
    replaceAssistantReview(pendingReview)
    if (pendingReview !== null) return
    const startNextRound = () => {
      if (assistantRunningRef.current) {
        window.setTimeout(startNextRound, 0)
        return
      }
      const [queuedTurn, ...remainingQueue] = assistantQueueRef.current
      if (queuedTurn === undefined) return
      replaceAssistantQueue(remainingQueue)
      void runAssistantRounds(queuedTurn)
    }
    window.setTimeout(startNextRound, 0)
  }, [replaceAssistantQueue, replaceAssistantReview, replaceDismissedEditKeys, runAssistantRounds])

  const fixCompileError = () => {
    if (status.state !== 'error' || assistantBusy) return
    void sendMessage(
      'Fix the current LaTeX compilation failure. Inspect the compiler diagnostics and relevant project files, then propose the smallest safe edit needed to make the manuscript compile. Preserve unrelated content.',
    )
  }

  const applyEdits = async (edits: ProposedEdit[]): Promise<boolean> => {
    const prompt = edits.length === 1
      ? `Apply Codex's edit and ${edits[0].is_new ? 'create' : 'replace'} ${edits[0].path}?`
      : `Apply all ${edits.length} Codex edits together?\n\n${edits.map((edit) => edit.path).join('\n')}`
    if (!window.confirm(prompt)) return false
    try {
      if (dirtyRef.current) await saveCurrent(false)
      setEditIntent(null)
      const result = await api.applyEdits(edits)
      await refreshTree()
      const saved = result.files.find((file) => file.path === pathRef.current) ?? result.files[0]
      updatePath(saved.path)
      fileVersionRef.current = saved.version ?? null
      syncedContentRef.current = saved.content
      externalWarningVersionRef.current = null
      updateContent(saved.content)
      updateDirty(false)
      setSelectedText('')
      setSelectedLine(null)
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
        <span className="wordmark">
          <img src="/cloverleaf.svg" alt="" aria-hidden="true" />
          cloverleaf
        </span>
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
                onLoadProject={() => setProjectPickerMode('load')}
                onNewProject={() => setProjectPickerMode('new')}
                loadDisabled={!project || assistantBusy || projectLoading}
                onCreate={createEntry}
                onRename={renameEntry}
                onDelete={deleteEntry}
              />
            </Panel>
            <PanelResizeHandle className="resize-handle horizontal" aria-label="Resize project and compiler panels" />
            <Panel defaultSize={33} minSize={20}>
              <ProjectStatus
                status={status}
                mainFile={project?.main_file ?? 'main.tex'}
                onCompile={saveAndCompile}
                onFixWithCodex={fixCompileError}
                codexBusy={assistantBusy}
              />
            </Panel>
          </PanelGroup>
        </Panel>
        <PanelResizeHandle className="resize-handle" aria-label="Resize project and editor panels" />
        <Panel defaultSize={49} minSize={28}>
          <Editor
            path={path}
            value={content}
            saveState={saving ? 'saving…' : dirty ? 'modified' : path ? 'saved' : '—'}
            isCompilationRoot={path === project?.main_file}
            compilationRootBusy={compilationRootBusy}
            sourceLocation={sourceLocation}
            onChange={editorChanged}
            onSelection={setSelectedText}
            onLineContext={setSelectedLine}
            onSaveAndCompile={saveAndCompile}
            onCompileView={compileOpenView}
          />
        </Panel>
        <PanelResizeHandle className="resize-handle" aria-label="Resize editor and preview panels" />
        <Panel defaultSize={33} minSize={23}>
          <PanelGroup direction="vertical" autoSaveId="cloverleaf-right">
            <Panel defaultSize={59} minSize={28}>
              <PdfPreview
                status={status}
                projectKey={project ? `${project.workspace}:${project.main_file}` : null}
                mainFile={project?.main_file ?? null}
                onError={showError}
                onLocateSource={locatePdfSource}
              />
            </Panel>
            <PanelResizeHandle className="resize-handle horizontal" aria-label="Resize preview and assistant panels" />
            <Panel defaultSize={41} minSize={24}>
              <Assistant
                key={assistantResetKey}
                messages={messages}
                busy={assistantBusy}
                cancelling={assistantCancelling}
                reviewMessageIndex={assistantReviewMessageIndex}
                dismissedEditKeys={dismissedEditKeys}
                queuedMessages={queuedMessages}
                progress={assistantProgress}
                availableFiles={availableFiles}
                selectedLine={selectedLine}
                onClearLineContext={() => { setSelectedLine(null); setSelectedText('') }}
                onSend={sendMessage}
                onCancel={cancelAssistant}
                onResolveEdits={resolveAssistantEdits}
                onApplyEdits={applyEdits}
              />
            </Panel>
          </PanelGroup>
        </Panel>
      </PanelGroup>
      {projectPickerMode && project && (
        <ProjectPicker
          currentProject={project}
          mode={projectPickerMode}
          onClose={() => setProjectPickerMode(null)}
          onLoad={loadProject}
          onCreate={createProject}
        />
      )}
      {toast && <div className="toast" role="alert">{toast}</div>}
    </main>
  )
}
