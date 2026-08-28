import { AlertTriangle, CheckCircle2, FileOutput, LoaderCircle, Play, Sparkles } from 'lucide-react'
import type { CompileStatus } from '../types'

type Props = {
  status: CompileStatus
  mainFile: string
  onCompile: () => void
  onFixWithCodex: () => void
  codexBusy: boolean
}

export function ProjectStatus({ status, mainFile, onCompile, onFixWithCodex, codexBusy }: Props) {
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
        <div className="compiler-root" title={mainFile}>
          <FileOutput size={12} />
          <span>{mainFile}</span>
        </div>
        {status.state === 'error' && (
          <button
            type="button"
            className="compiler-fix-button"
            onClick={onFixWithCodex}
            disabled={codexBusy}
            aria-label="Fix compilation with Codex"
          >
            {codexBusy ? <LoaderCircle className="spin" size={13} /> : <Sparkles size={13} />}
            {codexBusy ? 'Codex is working…' : 'Fix with Codex'}
          </button>
        )}
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
