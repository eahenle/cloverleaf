import { expect, test } from '@playwright/test'
import { clearAssistantHistory } from './history'

test('double-clicking PDF text opens and focuses the included source line after resizing', async ({ page, request }) => {
  await clearAssistantHistory(request)
  await page.goto('/')
  await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
  await page.setViewportSize({ width: 1600, height: 1000 })
  const response = await request.get('/api/pdf')
  const { getDocument } = await import('pdfjs-dist/legacy/build/pdf.mjs')
  const document = await getDocument({ data: new Uint8Array(await response.body()) }).promise
  try {
    const pdfPage = await document.getPage(1)
    const viewport = pdfPage.getViewport({ scale: 1 })
    const content = await pdfPage.getTextContent()
    const text = content.items.find(item => 'str' in item && item.str.includes('Edit this paragraph'))
    expect(text && 'str' in text).toBeTruthy()
    if (!text || !('str' in text)) throw new Error('Fixture paragraph missing')
    const canvas = page.getByTestId('pdf-page').first()
    const lookup = page.waitForResponse(response => response.url().endsWith('/api/pdf/source'), { timeout: 15_000 })
    await canvas.dblclick({ position: {
      x: (text.transform[4] + text.width / 2) / viewport.width * (await canvas.boundingBox())!.width,
      y: (viewport.height - text.transform[5] - text.height / 2) / viewport.height * (await canvas.boundingBox())!.height,
    } })
    const result = await lookup
    expect(result.ok(), await result.text()).toBeTruthy()
    const location = await result.json() as { path: string; line: number }
    expect(location.path).toBe('sections/introduction.tex')
    await expect(page.locator('.project-path')).toContainText(location.path)
    await expect(page.locator('.cm-editor')).toHaveClass(/cm-focused/)
    await expect(page.locator('.cm-lineNumbers .cm-activeLineGutter')).toHaveText(String(location.line))
    await expect(page.getByLabel('Codex line context')).toHaveCount(0)
  } finally { await document.destroy() }
})

test('source lookup failures preserve the editor and show a useful message', async ({ page }) => {
  await page.route('**/api/pdf/source', route => route.fulfill({ status: 409, json: { detail: 'The preview changed; recompile and try again' } }))
  await page.goto('/')
  await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
  await page.getByTestId('pdf-page').first().dblclick({ position: { x: 100, y: 150 } })
  await expect(page.getByRole('alert')).toContainText('The preview changed')
  await expect(page.locator('.project-path')).toContainText('main.tex')
})

test('source navigation saves the current draft and clamps a mapped line to the target document', async ({ page, request }) => {
  const original = await (await request.get('/api/files/main.tex')).json() as { content: string }
  const draft = `${original.content}\n% Preserved before PDF navigation\n`
  let point: { x: number; y: number } | undefined
  await page.route('**/api/pdf/source', route => {
    point = route.request().postDataJSON()
    return route.fulfill({ json: { path: 'sections/introduction.tex', line: 9999, column: 9999 } })
  })
  try {
    await page.goto('/')
    await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
    const editor = page.locator('.cm-content')
    await editor.press('ControlOrMeta+a')
    await page.keyboard.insertText(draft)
    await page.getByTestId('pdf-page').first().dblclick({ position: { x: 100, y: 150 } })
    await expect(page.locator('.project-path')).toContainText('sections/introduction.tex')
    await expect(page.locator('.cm-editor')).toHaveClass(/cm-focused/)
    const target = await (await request.get('/api/files/sections/introduction.tex')).json() as { content: string }
    await expect(page.locator('.cm-lineNumbers .cm-activeLineGutter')).toHaveText(String(target.content.split('\n').length))
    expect((await (await request.get('/api/files/main.tex')).json()).content).toBe(draft)
    expect(point!.x).toBeGreaterThan(100)
    expect(point!.y).toBeGreaterThan(150)
    await page.getByLabel('Message Codex').focus()
    await page.getByTestId('pdf-page').first().dblclick({ position: { x: 100, y: 150 } })
    await expect(page.locator('.cm-editor')).toHaveClass(/cm-focused/)
  } finally {
    await request.put('/api/files/main.tex', { data: { path: 'main.tex', content: original.content } })
    await request.post('/api/compile')
    await expect.poll(async () => (await (await request.get('/api/compile/status')).json()).state).toBe('success')
  }
})

test('choosing a file manually supersedes an outstanding PDF source lookup', async ({ page }) => {
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/pdf/source', async route => {
    await pending
    await route.fulfill({ json: { path: 'main.tex', line: 1, column: 0 } })
  })
  await page.goto('/')
  await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
  const lookup = page.waitForRequest(request => request.url().endsWith('/api/pdf/source'))
  await page.getByTestId('pdf-page').first().dblclick({ position: { x: 100, y: 150 } })
  await lookup
  await page.getByLabel('Expand sections').click()
  await page.getByRole('button', { name: 'introduction.tex', exact: true }).click()
  await expect(page.locator('.project-path')).toContainText('sections/introduction.tex')
  const response = page.waitForResponse(response => response.url().endsWith('/api/pdf/source'))
  release()
  await response
  await page.getByLabel('Message Codex').fill('Keep working in the chosen file')
  await expect(page.locator('.project-path')).toContainText('sections/introduction.tex')
})

test('typing while the mapped source loads cancels the jump and keeps the new draft', async ({ page, request }) => {
  const original = await (await request.get('/api/files/main.tex')).json() as { content: string }
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/pdf/source', route => route.fulfill({ json: { path: 'sections/introduction.tex', line: 3, column: 0 } }))
  await page.route('**/api/files/sections/introduction.tex', async route => {
    await pending
    await route.continue()
  })
  try {
    await page.goto('/')
    await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
    const loading = page.waitForRequest(request => request.url().endsWith('/api/files/sections/introduction.tex'))
    await page.getByTestId('pdf-page').first().dblclick({ position: { x: 100, y: 150 } })
    await loading
    await page.locator('.cm-content').press('ControlOrMeta+a')
    const draft = `${original.content}\n% Typed while PDF source was loading\n`
    await page.keyboard.insertText(draft)
    const response = page.waitForResponse(response => response.url().endsWith('/api/files/sections/introduction.tex'))
    release()
    await response
    await expect(page.locator('.cm-content')).toContainText('Typed while PDF source was loading')
    await expect(page.locator('.project-path')).toContainText('main.tex')
  } finally {
    release()
    await page.close()
    await request.put('/api/files/main.tex', { data: { path: 'main.tex', content: original.content } })
    await request.post('/api/compile')
    await expect.poll(async () => (await (await request.get('/api/compile/status')).json()).state).toBe('success')
  }
})

test('a jump immediately after typing positions the cursor in the new file rather than the old document', async ({ page }) => {
  await page.route('**/api/files/main.tex', route => route.request().method() === 'PUT'
    ? route.fulfill({ json: { path: 'main.tex', content: route.request().postDataJSON().content, version: 'saved-draft' } })
    : route.continue())
  await page.route('**/api/pdf/source', route => route.fulfill({ json: { path: 'sections/introduction.tex', line: 3, column: 0 } }))
  await page.goto('/')
  await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
  await page.locator('.cm-content').press('ControlOrMeta+a')
  await page.keyboard.insertText('A short draft')
  await page.getByTestId('pdf-page').first().dblclick({ position: { x: 100, y: 150 } })
  await expect(page.locator('.project-path')).toContainText('sections/introduction.tex')
  await expect(page.locator('.cm-content')).toContainText('Edit this paragraph')
  await expect(page.locator('.cm-lineNumbers .cm-activeLineGutter')).toHaveText('3')
  await expect(page.locator('.cm-editor')).toHaveClass(/cm-focused/)
})
