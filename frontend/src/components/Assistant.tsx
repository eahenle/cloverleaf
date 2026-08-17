import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from 'react'
import { Check, Send, X } from 'lucide-react'
import type { AssistantProgress, ChatMessage, ProposedEdit } from '../types'
import { ServerTerminal } from './ServerTerminal'

type Props = {
  messages: ChatMessage[]
  busy: boolean
  progress: AssistantProgress | null
  onSend: (message: string) => void
  onApplyEdits: (edits: ProposedEdit[]) => Promise<boolean>
}

export function Assistant({ messages, busy, progress, onSend, onApplyEdits }: Props) {
  const [draft, setDraft] = useState('')
  const [dismissed, setDismissed] = useState<Set<string>>(new Set())
  const [activeTab, setActiveTab] = useState<'codex' | 'terminal'>('codex')
  const [now, setNow] = useState(Date.now())
  const messageListRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const messageList = messageListRef.current
    if (messageList) messageList.scrollTop = messageList.scrollHeight
  }, [busy, dismissed, messages])

  useEffect(() => {
    if (!busy) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [busy])

  const submit = (event?: FormEvent) => {
    event?.preventDefault()
    const message = draft.trim()
    if (!message || busy) return
    setDraft('')
    onSend(message)
  }

  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
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
        </div>
        <span
          className={`status-dot ${busy ? 'compiling' : 'success'}`}
          title={busy ? progress?.message ?? 'Starting Codex' : 'Ready'}
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
                .filter(({ key }) => !dismissed.has(key))
              return (
                <div className={`message ${message.role}`} key={`${message.role}-${messageIndex}`}>
                  {message.content}
                  {visibleEdits.length > 1 && (
                    <div className="edit-batch-actions">
                      <button
                        type="button"
                        onClick={() => {
                          void onApplyEdits(visibleEdits.map(({ edit }) => edit)).then((applied) => {
                            if (!applied) return
                            setDismissed((current) => {
                              const next = new Set(current)
                              visibleEdits.forEach(({ key }) => next.add(key))
                              return next
                            })
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
                                if (applied) setDismissed((current) => new Set(current).add(key))
                              })
                            }}
                          >
                            <Check size={12} /> Apply
                          </button>
                          <button
                            type="button"
                            onClick={() => setDismissed((current) => new Set(current).add(key))}
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
            {busy && (
              <AssistantActivity progress={progress} now={now} />
            )}
          </div>
          <form className="chat-input" onSubmit={submit}>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={keyDown}
              placeholder="Ask Codex to edit or inspect…"
              aria-label="Message Codex"
              rows={2}
            />
            <button type="submit" disabled={busy || !draft.trim()} title="Send message" aria-label="Send message">
              <Send size={14} />
            </button>
          </form>
        </div>
      ) : (
        <ServerTerminal />
      )}
    </section>
  )
}

function AssistantActivity({ progress, now }: { progress: AssistantProgress | null; now: number }) {
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
    </div>
  )
}
