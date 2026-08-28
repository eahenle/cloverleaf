import { useEffect, useRef, useState } from 'react'
import { Power, RotateCcw } from 'lucide-react'
import { api } from '../api'
import type { RuntimeInfo } from '../types'

const MAX_OUTPUT_CHARACTERS = 200_000

export function ServerTerminal() {
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null)
  const [output, setOutput] = useState('')
  const [message, setMessage] = useState('Connecting to the Cloverleaf launcher…')
  const [shutdownBusy, setShutdownBusy] = useState(false)
  const [restartBusy, setRestartBusy] = useState(false)
  const outputRef = useRef<HTMLPreElement>(null)
  const followOutputRef = useRef(true)
  const shutdownRef = useRef(false)
  const restartRef = useRef(false)

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
            if (active && !shutdownRef.current && !restartRef.current) {
              setMessage('The server output stream disconnected.')
            }
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

  const restart = async () => {
    if (!window.confirm('Restart the Cloverleaf backend and frontend?')) return
    restartRef.current = true
    setRestartBusy(true)
    setOutput('')
    try {
      const result = await api.restartRuntime()
      setMessage(`${result.message}. Reconnecting…`)
      const startedAt = Date.now()
      let sawDisconnect = false
      while (Date.now() - startedAt < 20_000) {
        await new Promise((resolve) => window.setTimeout(resolve, 300))
        try {
          await api.health()
          if (sawDisconnect || Date.now() - startedAt >= 1_500) {
            setRuntime(await api.runtime())
            restartRef.current = false
            setRestartBusy(false)
            setMessage('')
            return
          }
        } catch {
          sawDisconnect = true
        }
      }
      throw new Error('The server did not become healthy after restarting.')
    } catch (reason) {
      restartRef.current = false
      setRestartBusy(false)
      setMessage(reason instanceof Error ? reason.message : 'Server restart failed.')
    }
  }

  return (
    <div className="panel-body server-terminal" role="tabpanel" aria-label="Server terminal">
      <div className="terminal-toolbar">
        <span>backend + frontend stdout/stderr</span>
        <div className="terminal-actions">
          <button
            type="button"
            className="terminal-control-button"
            disabled={!runtime?.restart_available || restartBusy || shutdownBusy}
            onClick={() => void restart()}
            title={runtime?.restart_available ? 'Restart Cloverleaf' : 'Start with ./cloverleaf to enable restart'}
          >
            <RotateCcw size={12} className={restartBusy ? 'spin' : ''} /> {restartBusy ? 'Restarting…' : 'Restart server'}
          </button>
          <button
            type="button"
            className="danger-button"
            disabled={!runtime?.shutdown_available || shutdownBusy || restartBusy}
            onClick={() => void shutdown()}
            title={runtime?.shutdown_available ? 'Shut down Cloverleaf' : 'Start with ./cloverleaf to enable shutdown'}
          >
            <Power size={12} /> {shutdownBusy ? 'Shutting down…' : 'Shut down server'}
          </button>
        </div>
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
