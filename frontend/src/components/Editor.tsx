import { useMemo } from 'react'
import CodeMirror from '@uiw/react-codemirror'
import { StreamLanguage } from '@codemirror/language'
import { stex } from '@codemirror/legacy-modes/mode/stex'
import { EditorView, keymap, lineNumbers } from '@codemirror/view'
import { LoaderCircle, Play, Save } from 'lucide-react'
import type { SelectedLine } from '../types'

type Props = {
  path: string | null
  value: string
  saveState: string
  isCompilationRoot: boolean
  compilationRootBusy: boolean
  onChange: (value: string) => void
  onSelection: (value: string) => void
  onLineContext: (line: SelectedLine) => void
  onSaveAndCompile: () => void
  onCompileView: () => void
}

export function Editor({
  path,
  value,
  saveState,
  isCompilationRoot,
  compilationRootBusy,
  onChange,
  onSelection,
  onLineContext,
  onSaveAndCompile,
  onCompileView,
}: Props) {
  const canCompileView = Boolean(path && /\.tex$/i.test(path) && !isCompilationRoot)
  const extensions = useMemo(
    () => [
      StreamLanguage.define(stex),
      EditorView.lineWrapping,
      lineNumbers({
        domEventHandlers: {
          mousedown: (view, block, event) => {
            if (!(event instanceof MouseEvent) || event.button !== 0 || !path) return false
            event.preventDefault()
            const line = view.state.doc.lineAt(block.from)
            onLineContext({ path, line: line.number, text: line.text })
            view.dispatch({ selection: { anchor: line.from, head: line.to } })
            return true
          },
        },
      }),
      EditorView.contentAttributes.of({
        'aria-label': path ? `Manuscript editor: ${path}` : 'Manuscript editor',
        spellcheck: 'true',
      }),
      EditorView.updateListener.of((update) => {
        if (update.selectionSet || update.docChanged) {
          const selection = update.state.selection.main
          onSelection(update.state.doc.sliceString(selection.from, selection.to))
        }
      }),
      keymap.of([
        {
          key: 'Mod-s',
          preventDefault: true,
          run: () => {
            onSaveAndCompile()
            return true
          },
        },
      ]),
    ],
    [onSaveAndCompile, onSelection, onLineContext, path],
  )

  return (
    <section className="panel editor-panel" aria-label="Source editor">
      <header className="panel-header">
        <span>{path ?? 'No file open'}</span>
        <span className="editor-status-group">
          {canCompileView && (
            <button
              className="text-button compile-view-button"
              type="button"
              onClick={onCompileView}
              disabled={compilationRootBusy}
              title="Use this file as the compilation root and render its PDF"
              aria-label={`Compile ${path} as the current view`}
            >
              {compilationRootBusy ? <LoaderCircle className="spin" size={11} /> : <Play size={11} />}
              {compilationRootBusy ? 'Switching…' : 'Compile this view'}
            </button>
          )}
          <span className="save-state" aria-live="polite">{saveState}</span>
          <button
            className="icon-button"
            type="button"
            onClick={onSaveAndCompile}
            disabled={!path}
            title="Save and compile (Cmd/Ctrl-S)"
            aria-label="Save and compile"
          >
            <Save size={14} />
          </button>
        </span>
      </header>
      <div className="panel-body">
        {path ? (
          <CodeMirror
            className="source-editor"
            value={value}
            height="100%"
            extensions={extensions}
            onChange={onChange}
            basicSetup={{
              lineNumbers: false,
              foldGutter: true,
              highlightActiveLine: true,
              highlightSelectionMatches: true,
              autocompletion: true,
              history: true,
              searchKeymap: true,
            }}
          />
        ) : (
          <div className="empty-state">Choose a text file from the project tree.</div>
        )}
      </div>
    </section>
  )
}
