import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from 'react'
import { Check, Send, X } from 'lucide-react'
import type { AssistantProgress, ChatMessage, ProposedEdit } from '../types'
import { ServerTerminal } from './ServerTerminal'

type Props = {
  messages: ChatMessage[]
  busy: boolean
  progress: AssistantProgress | null
  onSend: (message: string) => void
  onApplyEdit: (edit: ProposedEdit) => Promise<boolean>
}

export function Assistant({ messages, busy, progress, onSend, onApplyEdit }: Props) {
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
                Ask about the open manuscript, selected text, or active compiler diagnostics. Proposed edits always require review.
              </p>
            )}
            {messages.map((message, messageIndex) => (
              <div className={`message ${message.role}`} key={`${message.role}-${messageIndex}`}>
                {message.content}
                {message.edits?.map((edit, editIndex) => {
                  const key = `${messageIndex}-${editIndex}`
                  if (dismissed.has(key)) return null
                  return (
                    <div className="proposed-edit" key={key}>
                      <strong>{edit.path}</strong>
                      <span>{edit.summary}</span>
                      <div>
                        <button
                          type="button"
                          onClick={() => {
                            void onApplyEdit(edit).then((applied) => {
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
                    </div>
                  )
                })}
              </div>
            ))}
            {busy && (
              <AssistantActivity progress={progress} now={now} />
            )}
          </div>
          <form className="chat-input" onSubmit={submit}>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={keyDown}
              placeholder="Ask about the manuscript…"
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
