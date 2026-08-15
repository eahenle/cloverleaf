import { AlertTriangle, CheckCircle2, LoaderCircle, Play } from 'lucide-react'
import type { CompileStatus } from '../types'

type Props = {
  status: CompileStatus
  onCompile: () => void
}

export function ProjectStatus({ status, onCompile }: Props) {
  const StateIcon =
    status.state === 'success'
      ? CheckCircle2
      : status.state === 'compiling'
        ? LoaderCircle
        : AlertTriangle

  return (
    <section className="panel" aria-label="Compiler status">
      <header className="panel-header">
        <span>Compiler</span>
        <button
          type="button"
          className="text-button"
          onClick={onCompile}
          title="Save and compile"
          aria-label="Save and compile manuscript"
        >
          <Play size={11} /> Compile
        </button>
      </header>
      <div className="panel-body compiler-body">
        <div className={`compiler-state ${status.state}`} aria-live="polite">
          <StateIcon className={status.state === 'compiling' ? 'spin' : ''} size={15} />
          <span>{status.state === 'idle' ? 'Waiting for first build' : status.state}</span>
          {status.revision > 0 && <small>rev {status.revision}</small>}
        </div>
        <div className="diagnostics">
          {status.diagnostics.length === 0 ? (
            <p className="muted">
              {status.state === 'success' ? 'No compiler diagnostics.' : 'Diagnostics will appear here.'}
            </p>
          ) : (
            status.diagnostics.map((diagnostic, index) => (
              <div className={`diagnostic ${diagnostic.severity}`} key={`${diagnostic.message}-${index}`}>
                <span>{diagnostic.message}</span>
                {(diagnostic.file || diagnostic.line) && (
                  <strong>
                    {diagnostic.file ?? 'source'}{diagnostic.line ? `:${diagnostic.line}` : ''}
                  </strong>
                )}
              </div>
            ))
          )}
          {status.state === 'error' && status.log_tail && (
            <details className="build-log">
              <summary>Build log</summary>
              <pre>{status.log_tail}</pre>
            </details>
          )}
        </div>
      </div>
    </section>
  )
}
