import { useMemo } from 'react'
import CodeMirror from '@uiw/react-codemirror'
import { StreamLanguage } from '@codemirror/language'
import { stex } from '@codemirror/legacy-modes/mode/stex'
import { EditorView, keymap } from '@codemirror/view'
import { Save } from 'lucide-react'

type Props = {
  path: string | null
  value: string
  saveState: string
  onChange: (value: string) => void
  onSelection: (value: string) => void
  onSaveAndCompile: () => void
}

export function Editor({
  path,
  value,
  saveState,
  onChange,
  onSelection,
  onSaveAndCompile,
}: Props) {
  const extensions = useMemo(
    () => [
      StreamLanguage.define(stex),
      EditorView.lineWrapping,
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
    [onSaveAndCompile, onSelection, path],
  )

  return (
    <section className="panel editor-panel" aria-label="Source editor">
      <header className="panel-header">
        <span>{path ?? 'No file open'}</span>
        <span className="editor-status-group">
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
            value={value}
            height="100%"
            extensions={extensions}
            onChange={onChange}
            basicSetup={{
              lineNumbers: true,
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
