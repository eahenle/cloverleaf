import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronUp, Folder, HardDrive, Home, LoaderCircle, X } from 'lucide-react'
import { api } from '../api'
import type { DirectoryListing, ProjectInfo } from '../types'

type Props = {
  currentProject: ProjectInfo
  onClose: () => void
  onLoad: (workspace: string, mainFile: string) => Promise<void>
}

export function ProjectPicker({ currentProject, onClose, onLoad }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [listing, setListing] = useState<DirectoryListing | null>(null)
  const [mainFile, setMainFile] = useState('')
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const dismiss = () => {
    if (!submitting) onClose()
  }

  const navigate = useCallback(async (path: string) => {
    setLoading(true)
    setError('')
    try {
      const next = await api.browseDirectories(path)
      setListing(next)
      const preferred =
        next.path === currentProject.workspace && next.tex_files.includes(currentProject.main_file)
          ? currentProject.main_file
          : next.tex_files.includes('main.tex')
            ? 'main.tex'
            : next.tex_files[0] ?? 'main.tex'
      setMainFile(preferred)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }, [currentProject.main_file, currentProject.workspace])

  useEffect(() => {
    const dialog = dialogRef.current
    if (dialog && !dialog.open) dialog.showModal()
    void navigate(currentProject.workspace)
    return () => {
      if (dialog?.open) dialog.close()
    }
  }, [currentProject.workspace, navigate])

  const choose = async () => {
    if (!listing || !mainFile || submitting) return
    setSubmitting(true)
    setError('')
    try {
      await onLoad(listing.path, mainFile)
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="project-picker"
      aria-labelledby="project-picker-title"
      onCancel={(event) => {
        event.preventDefault()
        dismiss()
      }}
      onClick={(event) => {
        if (event.target === dialogRef.current) dismiss()
      }}
    >
      <div className="project-picker-card">
        <header>
          <div>
            <h2 id="project-picker-title">Load project</h2>
            <p>Choose a folder containing a LaTeX compilation root.</p>
          </div>
          <button
            type="button"
            onClick={dismiss}
            disabled={submitting}
            aria-label="Close project picker"
            title="Close"
          >
            <X size={16} />
          </button>
        </header>

        <div className="picker-toolbar">
          <button
            type="button"
            onClick={() => listing?.parent && void navigate(listing.parent)}
            disabled={!listing?.parent || loading}
            title="Parent folder"
          >
            <ChevronUp size={14} /> Up
          </button>
          <button
            type="button"
            onClick={() => listing && void navigate(listing.home)}
            disabled={!listing || loading}
            title="Home folder"
          >
            <Home size={14} /> Home
          </button>
          <button
            type="button"
            onClick={() => listing && void navigate(listing.root)}
            disabled={!listing || loading}
            title="Computer root"
          >
            <HardDrive size={14} /> Computer
          </button>
        </div>

        <div className="picker-location" title={listing?.path}>
          {listing?.path ?? currentProject.workspace}
        </div>

        <div className="picker-browser" aria-label="Folders" aria-busy={loading}>
          {loading && (
            <div className="picker-state">
              <LoaderCircle className="spin" size={18} /> Reading folders…
            </div>
          )}
          {!loading && listing?.directories.map((directory) => (
            <button
              type="button"
              className="picker-folder"
              key={directory.path}
              onClick={() => void navigate(directory.path)}
              aria-label={`Open folder ${directory.name}`}
            >
              <Folder size={16} />
              <span>{directory.name}</span>
            </button>
          ))}
          {!loading && listing?.directories.length === 0 && (
            <div className="picker-state">No subfolders.</div>
          )}
        </div>

        <label className="picker-root">
          <span>Compilation root</span>
          <select
            value={mainFile}
            onChange={(event) => setMainFile(event.target.value)}
            disabled={loading}
            aria-label="Compilation root"
          >
            {!listing?.tex_files.length && (
              <option value="main.tex">main.tex (will be created)</option>
            )}
            {listing?.tex_files.map((file) => <option key={file}>{file}</option>)}
          </select>
        </label>

        {error && <div className="picker-error" role="alert">{error}</div>}

        <footer>
          <button
            type="button"
            className="secondary-button"
            onClick={dismiss}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={() => void choose()}
            disabled={!listing || !mainFile || loading || submitting}
          >
            {submitting ? 'Loading…' : 'Open project'}
          </button>
        </footer>
      </div>
    </dialog>
  )
}
