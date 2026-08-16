import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist'
import workerSource from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import type { CompileStatus } from '../types'

GlobalWorkerOptions.workerSrc = workerSource

type PdfPageProps = {
  document: PDFDocumentProxy
  pageNumber: number
  width: number
  onRendered: () => void
  onError: (message: string) => void
}

function PdfPage({ document, pageNumber, width, onRendered, onError }: PdfPageProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    let cancelled = false
    let renderTask: RenderTask | null = null

    void document
      .getPage(pageNumber)
      .then((page) => {
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
        return renderTask.promise
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

  return <canvas ref={canvasRef} className="pdf-page" data-testid="pdf-page" aria-label={`PDF page ${pageNumber}`} />
}

type Props = { status: CompileStatus; projectKey: string | null }

export function PdfPreview({ status, projectKey }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const documentRef = useRef<PDFDocumentProxy | null>(null)
  const loadedRevisionRef = useRef(0)
  const scrollRatioRef = useRef(0)
  const restoredRevisionRef = useRef(0)
  const renderedPagesRef = useRef(new Set<number>())
  const [loaded, setLoaded] = useState<{ document: PDFDocumentProxy; revision: number } | null>(null)
  const [width, setWidth] = useState(320)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

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
      .then((data) => getDocument({ data }).promise)
      .then((nextDocument) => {
        if (cancelled) {
          void nextDocument.destroy()
          return
        }
        const previous = documentRef.current
        documentRef.current = nextDocument
        loadedRevisionRef.current = status.revision
        setLoaded({ document: nextDocument, revision: status.revision })
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

  const pageNumbers = loaded
    ? Array.from({ length: loaded.document.numPages }, (_, index) => index + 1)
    : []

  return (
    <section className="panel" aria-label="PDF preview">
      <header className="panel-header">
        <span>PDF preview</span>
        {loaded && (
          <span className="preview-pages">
            {loaded.document.numPages} {loaded.document.numPages === 1 ? 'page' : 'pages'}
          </span>
        )}
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
