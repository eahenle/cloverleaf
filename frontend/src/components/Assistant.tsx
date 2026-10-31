import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Check, Paperclip, Send, Square, X } from 'lucide-react'
import type { AssistantProgress, ChatMessage, ProposedEdit, SelectedLine } from '../types'
import { ServerTerminal } from './ServerTerminal'
import { SystemStatus } from './SystemStatus'

type Props = {
  messages: ChatMessage[]
  busy: boolean
  cancelling: boolean
  reviewMessageIndex: number | null
  dismissedEditKeys: Set<string>
  queuedMessages: string[]
  progress: AssistantProgress | null
  availableFiles: string[]
  selectedLine: SelectedLine | null
  onClearLineContext: () => void
  onSend: (message: string, attachedFiles: string[]) => void
  onCancel: () => void
  onResolveEdits: (editKeys: string[]) => void
  onApplyEdits: (edits: ProposedEdit[]) => Promise<boolean>
}

export function Assistant({
  messages,
  busy,
  cancelling,
  reviewMessageIndex,
  dismissedEditKeys,
  queuedMessages,
  progress,
  availableFiles,
  selectedLine,
  onClearLineContext,
  onSend,
  onCancel,
  onResolveEdits,
  onApplyEdits,
}: Props) {
  const [draft, setDraft] = useState('')
  const [attachedFiles, setAttachedFiles] = useState<string[]>([])
  const [activeTab, setActiveTab] = useState<'codex' | 'terminal' | 'status'>('codex')
  const [historyIndex, setHistoryIndex] = useState<number | null>(null)
  const [historyDraft, setHistoryDraft] = useState('')
  const [now, setNow] = useState(Date.now())
  const messageListRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const userHistory = useMemo(
    () => [
      ...messages.filter((message) => message.role === 'user').map((message) => message.content),
      ...queuedMessages,
    ],
    [messages, queuedMessages],
  )

  useEffect(() => {
    setAttachedFiles((current) => {
      const available = new Set(availableFiles)
      const next = current.filter((file) => available.has(file))
      return next.length === current.length ? current : next
    })
  }, [availableFiles])

  useEffect(() => {
    const messageList = messageListRef.current
    if (messageList) messageList.scrollTop = messageList.scrollHeight
  }, [busy, dismissedEditKeys, messages, queuedMessages])

  useEffect(() => {
    if (!busy) return
    setActiveTab('codex')
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [busy])

  const submit = (event?: FormEvent) => {
    event?.preventDefault()
    const message = draft.trim()
    if (!message || cancelling) return
    setDraft('')
    setHistoryIndex(null)
    setHistoryDraft('')
    onSend(message, attachedFiles)
    onClearLineContext()
    setAttachedFiles([])
  }

  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'ArrowUp' && !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey) {
      if (userHistory.length === 0) return
      event.preventDefault()
      const nextIndex = historyIndex === null
        ? userHistory.length - 1
        : Math.max(0, historyIndex - 1)
      if (historyIndex === null) setHistoryDraft(draft)
      setHistoryIndex(nextIndex)
      setDraft(userHistory[nextIndex])
      window.requestAnimationFrame(() => {
        const input = inputRef.current
        input?.setSelectionRange(input.value.length, input.value.length)
      })
      return
    }
    if (event.key === 'ArrowDown' && historyIndex !== null && !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey) {
      event.preventDefault()
      if (historyIndex < userHistory.length - 1) {
        const nextIndex = historyIndex + 1
        setHistoryIndex(nextIndex)
        setDraft(userHistory[nextIndex])
      } else {
        setHistoryIndex(null)
        setDraft(historyDraft)
      }
      window.requestAnimationFrame(() => {
        const input = inputRef.current
        input?.setSelectionRange(input.value.length, input.value.length)
      })
      return
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <section className="panel" aria-label="Codex assistant">
      <header className="panel-header assistant-header">
        <div className="panel-tabs" role="tablist" aria-label="Codex panel views">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'codex'}
            onClick={() => setActiveTab('codex')}
          >
            Codex
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'terminal'}
            onClick={() => setActiveTab('terminal')}
          >
            Terminal
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'status'}
            onClick={() => setActiveTab('status')}
          >
            Status
          </button>
        </div>
        <span
          className={`status-dot ${busy || reviewMessageIndex !== null ? 'compiling' : 'success'}`}
          title={busy ? progress?.message ?? 'Starting Codex' : reviewMessageIndex !== null ? 'Waiting for edit review' : 'Ready'}
        />
      </header>
      {activeTab === 'codex' ? (
        <div className="panel-body chat" role="tabpanel" aria-label="Codex conversation">
          <div className="messages" aria-live="polite" ref={messageListRef}>
            {messages.length === 0 && (
              <p className="assistant-intro">
                Ask Codex to inspect or edit the LaTeX project. File changes arrive as reviewable edits, never hidden workspace mutations.
              </p>
            )}
            {messages.map((message, messageIndex) => {
              const visibleEdits = (message.edits ?? [])
                .map((edit, editIndex) => ({ edit, key: `${messageIndex}-${editIndex}` }))
                .filter(({ key }) => !dismissedEditKeys.has(key))
              return (
                <div className={`message ${message.role}`} key={`${message.role}-${messageIndex}`}>
                  {message.content}
                  {(message.warnings ?? []).map((warning, warningIndex) => (
                    <div className="assistant-warning" role="status" key={warningIndex}>
                      {warning}
                    </div>
                  ))}
                  {visibleEdits.length > 1 && (
                    <div className="edit-batch-actions">
                      <button
                        type="button"
                        onClick={() => {
                          void onApplyEdits(visibleEdits.map(({ edit }) => edit)).then((applied) => {
                            if (!applied) return
                            onResolveEdits(visibleEdits.map(({ key }) => key))
                          })
                        }}
                      >
                        <Check size={12} /> Apply all {visibleEdits.length} edits
                      </button>
                    </div>
                  )}
                  {visibleEdits.map(({ edit, key }) => {
                    return (
                      <div className="proposed-edit" key={key}>
                        <strong>{edit.path}</strong>
                        <span>{edit.summary}</span>
                        <div className="edit-actions">
                          <button
                            type="button"
                            onClick={() => {
                              void onApplyEdits([edit]).then((applied) => {
                                if (!applied) return
                                onResolveEdits([key])
                              })
                            }}
                          >
                            <Check size={12} /> Apply
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              onResolveEdits([key])
                            }}
                          >
                            <X size={12} /> Dismiss
                          </button>
                        </div>
                        <details
                          className="edit-review"
                          onToggle={(event) => {
                            if (event.currentTarget.open) {
                              const card = event.currentTarget.parentElement
                              window.requestAnimationFrame(() => {
                                card?.scrollIntoView({ block: 'nearest' })
                              })
                            }
                          }}
                        >
                          <summary>Review {edit.replacements?.length ? 'changes' : edit.is_new ? 'new file' : 'replacement'}</summary>
                          {edit.replacements?.length ? (
                            <div className="replacement-list">
                              {edit.replacements.map((replacement, replacementIndex) => (
                                <div className="replacement-pair" key={replacementIndex}>
                                  {edit.replacements && edit.replacements.length > 1 && (
                                    <strong>Change {replacementIndex + 1}</strong>
                                  )}
                                  {!edit.is_new && (
                                    <>
                                      <span className="replacement-label removed">Before</span>
                                      <pre>{replacement.old_text}</pre>
                                    </>
                                  )}
                                  <span className="replacement-label added">{edit.is_new ? 'New file' : 'After'}</span>
                                  <pre>{replacement.new_text}</pre>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <pre>{edit.content}</pre>
                          )}
                        </details>
                      </div>
                    )
                  })}
                </div>
              )
            })}
            {reviewMessageIndex !== null && (
              <div className="assistant-review-pause" role="status">
                <strong>Review edits before the next Codex round</strong>
                <span>
                  Apply or dismiss every pending edit. {queuedMessages.length > 0
                    ? `${queuedMessages.length} queued round${queuedMessages.length === 1 ? '' : 's'} will start automatically afterward.`
                    : 'New prompts will wait here until the review is settled.'}
                </span>
              </div>
            )}
            {busy && (
              <AssistantActivity
                progress={progress}
                now={now}
                cancelling={cancelling}
                onCancel={onCancel}
              />
            )}
            {queuedMessages.map((message, index) => (
              <div className="message user queued" key={`queued-${index}`}>
                {message}
                <span className="queued-label" role="status">
                  Queued · round {index + 1}
                </span>
              </div>
            ))}
          </div>
          <form className="chat-input" onSubmit={submit}>
            {selectedLine && (
              <div className="assistant-attachments" aria-label="Codex line context">
                <span title={selectedLine.text}>
                  {selectedLine.path}:{selectedLine.line}
                  <button type="button" aria-label="Clear line context" onClick={onClearLineContext}>
                    <X size={11} />
                  </button>
                </span>
              </div>
            )}
            {attachedFiles.length > 0 && (
              <div className="assistant-attachments" aria-label="Attached files">
                {attachedFiles.map((file) => (
                  <span key={file}>
                    {file}
                    <button
                      type="button"
                      aria-label={`Remove ${file}`}
                      onClick={() => setAttachedFiles((current) => current.filter((item) => item !== file))}
                    >
                      <X size={11} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <textarea
              ref={inputRef}
              value={draft}
              onChange={(event) => {
                setDraft(event.target.value)
                setHistoryIndex(null)
                setHistoryDraft('')
              }}
              onKeyDown={keyDown}
              placeholder={cancelling ? 'Waiting for Codex to stop…' : reviewMessageIndex !== null ? 'Queue a message until edits are reviewed…' : busy ? 'Queue a message for the next round…' : 'Ask Codex to edit or inspect…'}
              aria-label="Message Codex"
              rows={2}
            />
            <label className="attach-picker" title="Attach a project file">
              <Paperclip size={14} aria-hidden="true" />
              <span className="sr-only">Attach a project file</span>
              <select
                aria-label="Attach a project file"
                value=""
                disabled={cancelling || attachedFiles.length >= 20}
                onChange={(event) => {
                  const file = event.target.value
                  if (file) {
                    setAttachedFiles((current) => current.includes(file) ? current : [...current, file])
                  }
                }}
              >
                <option value="">Attach file…</option>
                {availableFiles.filter((file) => !attachedFiles.includes(file)).map((file) => (
                  <option key={file} value={file}>{file}</option>
                ))}
              </select>
            </label>
            <button
              type="submit"
              disabled={cancelling || !draft.trim()}
              title={busy || reviewMessageIndex !== null ? 'Queue message for next round' : 'Send message'}
              aria-label="Send message"
            >
              <Send size={14} />
            </button>
          </form>
        </div>
      ) : activeTab === 'terminal' ? (
        <ServerTerminal />
      ) : (
        <SystemStatus />
      )}
    </section>
  )
}

function AssistantActivity({
  progress,
  now,
  cancelling,
  onCancel,
}: {
  progress: AssistantProgress | null
  now: number
  cancelling: boolean
  onCancel: () => void
}) {
  const startedAt = progress?.started_at ?? now
  const elapsedSeconds = Math.max(0, Math.floor((now - startedAt) / 1_000))
  const signalAge = Math.max(0, Math.floor((now - (progress?.received_at ?? now)) / 1_000))
  const signalState = signalAge < 6 ? 'live' : signalAge < 15 ? 'delayed' : 'stale'
  const signalLabel = signalAge < 2 ? 'stream live' : `last signal ${signalAge}s ago`

  return (
    <div className="assistant-activity" role="status" aria-label="Codex activity">
      <span className="activity-pulse" aria-hidden="true" />
      <div>
        <strong>{progress?.message ?? 'Starting Codex…'}</strong>
        <span className="activity-phase">{progress?.phase ?? 'starting'}</span>
        <small>
          <span>{elapsedSeconds}s elapsed</span>
          <span className={`activity-health ${signalState}`}>{signalLabel}</span>
          <span>{progress?.activity_count ?? 0} runtime updates</span>
        </small>
      </div>
      <button
        type="button"
        className="cancel-turn-button"
        onClick={onCancel}
        disabled={cancelling}
        aria-label="Cancel Codex turn"
      >
        <Square size={9} /> {cancelling ? 'Stopping…' : 'Cancel'}
      </button>
    </div>
  )
}
