import { FormEvent, KeyboardEvent, useState } from 'react'
import { Check, Send, X } from 'lucide-react'
import type { ChatMessage, ProposedEdit } from '../types'

type Props = {
  messages: ChatMessage[]
  busy: boolean
  onSend: (message: string) => void
  onApplyEdit: (edit: ProposedEdit) => Promise<boolean>
}

export function Assistant({ messages, busy, onSend, onApplyEdit }: Props) {
  const [draft, setDraft] = useState('')
  const [dismissed, setDismissed] = useState<Set<string>>(new Set())

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
      <header className="panel-header">
        <span>Codex assistant</span>
        <span className={`status-dot ${busy ? 'compiling' : 'success'}`} title={busy ? 'Thinking' : 'Ready'} />
      </header>
      <div className="panel-body chat">
        <div className="messages" aria-live="polite">
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
          {busy && <div className="message thinking">Codex is reading the manuscript context…</div>}
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
    </section>
  )
}
