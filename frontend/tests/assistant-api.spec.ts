import { expect, test } from '@playwright/test'
import { api } from '../src/api'
import type { AssistantContext, ChatMessage } from '../src/types'

const context: AssistantContext = {
  main_file: 'main.tex',
  open_file: 'main.tex',
  open_file_content: 'Open editor draft',
  selected_text: null,
  diagnostics: [],
  compile_state: 'idle',
  compile_log: '',
}

test.describe('assistant request transport', () => {
  let requests: RequestInit[]
  let sockets: Array<{ closed: boolean }>
  const originalGlobals = new Map<string, PropertyDescriptor | undefined>()

  test.beforeEach(() => {
    requests = []
    sockets = []
    const replacements = {
      window: { location: { protocol: 'http:', host: 'localhost' }, setTimeout, clearTimeout },
      WebSocket: class {
        onopen?: () => void
        closed = false
        constructor() {
          sockets.push(this)
          queueMicrotask(() => this.onopen?.())
        }
        close() { this.closed = true }
      },
      fetch: async (_path: string, init: RequestInit) => {
        requests.push(init)
        return Response.json({ message: 'Done.', proposed_edits: [] })
      },
    }
    for (const [name, value] of Object.entries(replacements)) {
      originalGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name))
      Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
    }
  })

  test.afterEach(() => {
    for (const [name, descriptor] of originalGlobals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else Reflect.deleteProperty(globalThis, name)
    }
    originalGlobals.clear()
  })

  test('sends selected project paths without browser-supplied attachment contents', async () => {
    const messages: ChatMessage[] = [{ role: 'user', content: 'Inspect these files' }]
    const controller = new AbortController()
    const result = await api.chat(
      messages, context, 123, () => undefined, 'attachment-turn', controller.signal,
      ['main.tex', 'sections/intro.tex'],
    )

    expect(result.message).toBe('Done.')
    expect(requests).toHaveLength(1)
    expect(JSON.parse(String(requests[0].body))).toEqual({
      messages,
      context,
      request_id: 'attachment-turn',
      attached_paths: ['main.tex', 'sections/intro.tex'],
    })
    expect(requests[0].signal).toBe(controller.signal)
    expect(sockets).toHaveLength(1)
    expect(sockets[0].closed).toBe(true)
  })

  test('a turn with no selection explicitly sends no attachments', async () => {
    await api.chat([], context, 123, () => undefined, 'plain-turn', new AbortController().signal, [])

    expect(JSON.parse(String(requests[0].body)).attached_paths).toEqual([])
  })

  test('cancelling before dispatch still prevents attachment requests', async () => {
    const controller = new AbortController()
    controller.abort()
    await expect(api.chat(
      [], context, 123, () => undefined, 'cancelled-turn', controller.signal, ['main.tex'],
    )).rejects.toThrow('Codex turn cancelled')
    expect(requests).toHaveLength(0)
    expect(sockets).toHaveLength(0)
  })
})
