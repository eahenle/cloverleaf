import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Download, LoaderCircle } from 'lucide-react'
import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist'
import workerSource from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import type { CompileStatus } from '../types'

GlobalWorkerOptions.workerSrc = workerSource

type PdfDestination = string | unknown[]
type PdfLink = {
  id: string
  url?: string
  destination?: PdfDestination
  left: number
  top: number
  width: number
  height: number
}

type PdfPageProps = {
  document: PDFDocumentProxy
  pageNumber: number
  width: number
  onNavigate: (destination: PdfDestination) => void
  onRendered: () => void
  onError: (message: string) => void
}

function PdfPage({ document, pageNumber, width, onNavigate, onRendered, onError }: PdfPageProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [links, setLinks] = useState<PdfLink[]>([])

  useEffect(() => {
    let cancelled = false
    let renderTask: RenderTask | null = null
    setLinks([])

    void document
      .getPage(pageNumber)
      .then(async (page) => {
        if (cancelled || !canvasRef.current) return
        const base = page.getViewport({ scale: 1 })
        const viewport = page.getViewport({ scale: width / base.width })
        const outputScale = Math.min(window.devicePixelRatio || 1, 2)
        const canvas = canvasRef.current
        const context = canvas.getContext('2d')
        if (!context) throw new Error('Canvas rendering is unavailable')
        canvas.width = Math.floor(viewport.width * outputScale)
        canvas.height = Math.floor(viewport.height * outputScale)
        canvas.style.width = `${Math.floor(viewport.width)}px`
        canvas.style.height = `${Math.floor(viewport.height)}px`
        renderTask = page.render({
          canvasContext: context,
          viewport,
          transform: outputScale === 1 ? undefined : [outputScale, 0, 0, outputScale, 0, 0],
        })
        const [, annotations] = await Promise.all([renderTask.promise, page.getAnnotations({ intent: 'display' })])
        if (cancelled) return
        const nextLinks: PdfLink[] = []
        for (const annotation of annotations) {
          if (annotation.subtype !== 'Link' || !Array.isArray(annotation.rect)) continue
          const url = typeof annotation.url === 'string' && /^(https?:|mailto:|tel:)/i.test(annotation.url)
            ? annotation.url : undefined
          const destination = typeof annotation.dest === 'string' || Array.isArray(annotation.dest)
            ? annotation.dest as PdfDestination : undefined
          if (!url && !destination) continue
          const [x1, y1, x2, y2] = viewport.convertToViewportRectangle(annotation.rect)
          nextLinks.push({
            id: annotation.id, url, destination,
            left: Math.min(x1, x2) / viewport.width * 100,
            top: Math.min(y1, y2) / viewport.height * 100,
            width: Math.abs(x2 - x1) / viewport.width * 100,
            height: Math.abs(y2 - y1) / viewport.height * 100,
          })
        }
        setLinks(nextLinks)
      })
      .then(() => {
        if (!cancelled) onRendered()
      })
      .catch((reason: unknown) => {
        if (!cancelled && !(reason instanceof Error && reason.name === 'RenderingCancelledException')) {
          onError(reason instanceof Error ? reason.message : String(reason))
        }
      })

    return () => {
      cancelled = true
      renderTask?.cancel()
    }
  }, [document, onError, onRendered, pageNumber, width])

  return (
    <div className="pdf-page-container" data-page-number={pageNumber}>
      <canvas ref={canvasRef} className="pdf-page" data-testid="pdf-page" aria-label={`PDF page ${pageNumber}`} />
      {links.map(link => (
        <a
          key={link.id}
          className="pdf-link"
          href={link.url ?? '#pdf-destination'}
          target={link.url ? '_blank' : undefined}
          rel={link.url ? 'noopener noreferrer' : undefined}
          aria-label={link.url ?? 'Go to document destination'}
          title={link.url ?? 'Go to document destination'}
          style={{ left: `${link.left}%`, top: `${link.top}%`, width: `${link.width}%`, height: `${link.height}%` }}
          onClick={event => {
            if (!link.url && link.destination) {
              event.preventDefault()
              onNavigate(link.destination)
            }
          }}
        />
      ))}
    </div>
  )
}

type WritablePdfFile = {
  write: (data: Blob) => Promise<void>
  close: () => Promise<void>
}

type PdfFileHandle = {
  createWritable: () => Promise<WritablePdfFile>
}

type SaveFilePicker = (options: {
  suggestedName: string
  types: Array<{ description: string; accept: Record<string, string[]> }>
}) => Promise<PdfFileHandle>

type Props = {
  status: CompileStatus
  projectKey: string | null
  mainFile: string | null
  onError: (reason: unknown) => void
}

export function PdfPreview({ status, projectKey, mainFile, onError }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const documentRef = useRef<PDFDocumentProxy | null>(null)
  const loadedRevisionRef = useRef(0)
  const scrollRatioRef = useRef(0)
  const restoredRevisionRef = useRef(0)
  const renderedPagesRef = useRef(new Set<number>())
  const [loaded, setLoaded] = useState<{
    document: PDFDocumentProxy
    revision: number
    data: ArrayBuffer
  } | null>(null)
  const [width, setWidth] = useState(320)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const pdfName = (mainFile?.split('/').pop() ?? 'document.tex').replace(/\.tex$/i, '.pdf')

  useLayoutEffect(() => {
    const element = scrollRef.current
    if (!element) return
    const measure = () => setWidth(Math.max(220, element.clientWidth - 28))
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const previous = documentRef.current
    documentRef.current = null
    loadedRevisionRef.current = 0
    restoredRevisionRef.current = 0
    scrollRatioRef.current = 0
    renderedPagesRef.current = new Set()
    setLoaded(null)
    setError('')
    if (previous) void previous.destroy()
  }, [projectKey])

  useEffect(() => {
    if (
      status.state !== 'success' ||
      status.revision <= 0 ||
      loadedRevisionRef.current === status.revision
    ) return
    const scroll = scrollRef.current
    if (scroll && scroll.scrollHeight > scroll.clientHeight) {
      scrollRatioRef.current = scroll.scrollTop / (scroll.scrollHeight - scroll.clientHeight)
    }
    let cancelled = false
    setLoading(true)
    setError('')
    renderedPagesRef.current = new Set()
    void fetch(`/api/pdf?revision=${status.revision}`, { cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) {
          const payload = (await response.json().catch(() => ({}))) as { detail?: string }
          throw new Error(payload.detail ?? `${response.status} ${response.statusText}`)
        }
        return response.arrayBuffer()
      })
      .then(async (data) => ({
        data,
        document: await getDocument({ data: data.slice(0) }).promise,
      }))
      .then(({ data, document: nextDocument }) => {
        if (cancelled) {
          void nextDocument.destroy()
          return
        }
        const previous = documentRef.current
        documentRef.current = nextDocument
        loadedRevisionRef.current = status.revision
        setLoaded({ document: nextDocument, revision: status.revision, data })
        if (previous) void previous.destroy()
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [status.revision, status.state])

  useEffect(
    () => () => {
      if (documentRef.current) void documentRef.current.destroy()
    },
    [],
  )

  const onPageRendered = useCallback(
    (pageNumber: number) => {
      if (!loaded) return
      renderedPagesRef.current.add(pageNumber)
      if (
        renderedPagesRef.current.size === loaded.document.numPages &&
        restoredRevisionRef.current !== loaded.revision
      ) {
        restoredRevisionRef.current = loaded.revision
        requestAnimationFrame(() => {
          const scroll = scrollRef.current
          if (scroll) {
            scroll.scrollTop = scrollRatioRef.current * Math.max(0, scroll.scrollHeight - scroll.clientHeight)
          }
        })
      }
    },
    [loaded],
  )

  const navigateToDestination = useCallback((destination: PdfDestination) => {
    if (!loaded) return
    void (async () => {
      const explicit = typeof destination === 'string'
        ? await loaded.document.getDestination(destination) : destination
      if (!explicit?.length) return
      const reference = explicit[0]
      const pageIndex = typeof reference === 'number'
        ? reference : await loaded.document.getPageIndex(reference as { num: number; gen: number })
      const scroll = scrollRef.current
      const target = scroll?.querySelector<HTMLElement>(`[data-page-number="${pageIndex + 1}"]`)
      if (scroll && target) {
        scroll.scrollTop += target.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 14
      }
    })().catch(reason => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [loaded])

  const pageNumbers = loaded
    ? Array.from({ length: loaded.document.numPages }, (_, index) => index + 1)
    : []

  const canSave = Boolean(
    loaded && status.state === 'success' && loaded.revision === status.revision,
  )

  const savePdf = useCallback(async () => {
    if (!loaded || !canSave || saving) return
    const picker = (window as unknown as { showSaveFilePicker?: SaveFilePicker }).showSaveFilePicker
    setSaving(true)
    try {
      const handle = picker
        ? await picker({
            suggestedName: pdfName,
            types: [{ description: 'PDF document', accept: { 'application/pdf': ['.pdf'] } }],
          })
        : null
      const blob = new Blob([loaded.data], { type: 'application/pdf' })
      if (handle) {
        const writable = await handle.createWritable()
        await writable.write(blob)
        await writable.close()
      } else {
        const url = URL.createObjectURL(blob)
        const anchor = document.createElement('a')
        anchor.href = url
        anchor.download = pdfName
        document.body.append(anchor)
        anchor.click()
        anchor.remove()
        window.setTimeout(() => URL.revokeObjectURL(url), 0)
      }
    } catch (reason) {
      if (!(reason instanceof DOMException && reason.name === 'AbortError')) onError(reason)
    } finally {
      setSaving(false)
    }
  }, [canSave, loaded, onError, pdfName, saving])

  return (
    <section className="panel" aria-label="PDF preview">
      <header className="panel-header">
        <span>PDF preview</span>
        <span className="preview-actions">
          {loaded && (
            <span className="preview-pages">
              {loaded.document.numPages} {loaded.document.numPages === 1 ? 'page' : 'pages'}
            </span>
          )}
          <button
            type="button"
            className="text-button preview-save-button"
            onClick={() => void savePdf()}
            disabled={!canSave || saving}
            title={`Save ${pdfName} to a chosen location`}
            aria-label={`Save PDF as ${pdfName}`}
          >
            {saving ? <LoaderCircle className="spin" size={13} /> : <Download size={13} />}
            {saving ? 'Saving…' : 'Save PDF'}
          </button>
        </span>
      </header>
      <div className="panel-body preview-body">
        <div className="pdf-scroll" ref={scrollRef} aria-busy={loading || status.state === 'compiling'}>
          {loaded &&
            pageNumbers.map((pageNumber) => (
              <PdfPage
                key={`${loaded.revision}-${pageNumber}`}
                document={loaded.document}
                pageNumber={pageNumber}
                width={width}
                onNavigate={navigateToDestination}
                onRendered={() => onPageRendered(pageNumber)}
                onError={setError}
              />
            ))}
        </div>
        {!loaded && !error && (
          <div className="preview-message">
            {status.state === 'error' ? 'No successful PDF build is available.' : 'Compiling the first preview…'}
          </div>
        )}
        {error && <div className="preview-message error">Could not render the PDF: {error}</div>}
        {loaded && status.state === 'compiling' && <div className="preview-badge">Recompiling…</div>}
        {loaded && status.state === 'error' && <div className="preview-badge error">Showing the last successful build</div>}
      </div>
    </section>
  )
}
