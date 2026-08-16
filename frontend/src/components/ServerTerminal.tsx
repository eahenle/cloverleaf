import { useEffect, useRef, useState } from 'react'
import { Power } from 'lucide-react'
import { api } from '../api'
import type { RuntimeInfo } from '../types'

const MAX_OUTPUT_CHARACTERS = 200_000

export function ServerTerminal() {
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null)
  const [output, setOutput] = useState('')
  const [message, setMessage] = useState('Connecting to the Cloverleaf launcher…')
  const [shutdownBusy, setShutdownBusy] = useState(false)
  const outputRef = useRef<HTMLPreElement>(null)
  const followOutputRef = useRef(true)
  const shutdownRef = useRef(false)

  useEffect(() => {
    let active = true
    let stopLogs: (() => void) | undefined
    void api.runtime()
      .then((info) => {
        if (!active) return
        setRuntime(info)
        if (!info.managed) {
          setMessage('Start Cloverleaf with ./cloverleaf to capture runtime output and enable shutdown.')
          return
        }
        if (!info.log_available) {
          setMessage('The launcher is running, but no server output is available yet.')
          return
        }
        setMessage('')
        stopLogs = api.watchRuntimeLogs(
          (chunk) => {
            if (!active) return
            setOutput((current) => `${current}${chunk}`.slice(-MAX_OUTPUT_CHARACTERS))
          },
          () => {
            if (active && !shutdownRef.current) setMessage('The server output stream disconnected.')
          },
        )
      })
      .catch((reason: unknown) => {
        if (active) setMessage(reason instanceof Error ? reason.message : 'Runtime status is unavailable.')
      })
    return () => {
      active = false
      stopLogs?.()
    }
  }, [])

  useEffect(() => {
    const terminal = outputRef.current
    if (terminal && followOutputRef.current) terminal.scrollTop = terminal.scrollHeight
  }, [output])

  const shutdown = async () => {
    if (!window.confirm('Shut down the Cloverleaf backend and frontend?')) return
    shutdownRef.current = true
    setShutdownBusy(true)
    try {
      const result = await api.shutdownRuntime()
      setMessage(`${result.message}. This page will disconnect.`)
    } catch (reason) {
      shutdownRef.current = false
      setShutdownBusy(false)
      setMessage(reason instanceof Error ? reason.message : 'Server shutdown failed.')
    }
  }

  return (
    <div className="panel-body server-terminal" role="tabpanel" aria-label="Server terminal">
      <div className="terminal-toolbar">
        <span>backend + frontend stdout/stderr</span>
        <button
          type="button"
          className="danger-button"
          disabled={!runtime?.shutdown_available || shutdownBusy}
          onClick={() => void shutdown()}
          title={runtime?.shutdown_available ? 'Shut down Cloverleaf' : 'Start with ./cloverleaf to enable shutdown'}
        >
          <Power size={12} /> {shutdownBusy ? 'Shutting down…' : 'Shut down server'}
        </button>
      </div>
      {message && <div className="terminal-message">{message}</div>}
      <pre
        className="terminal-output"
        ref={outputRef}
        aria-label="Server output"
        onScroll={(event) => {
          const terminal = event.currentTarget
          followOutputRef.current =
            terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 24
        }}
      >
        {output || (message ? '' : 'Waiting for server output…')}
      </pre>
    </div>
  )
}
