import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { mkdtemp, mkdir, realpath, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { clearAssistantHistory } from './history'

test.beforeEach(async ({ request }) => clearAssistantHistory(request))

type Status = { state: string; revision: number }

async function status(request: APIRequestContext): Promise<Status> {
  return (await (await request.get('/api/compile/status')).json()) as Status
}

async function waitForSuccessfulRevision(request: APIRequestContext, after: number): Promise<number> {
  await expect.poll(async () => {
    const current = await status(request)
    return current.state === 'success' ? current.revision : 0
  }, { timeout: 30_000 }).toBeGreaterThan(after)
  return (await status(request)).revision
}

async function pdfText(request: APIRequestContext): Promise<string> {
  const response = await request.get('/api/pdf')
  expect(response.ok()).toBeTruthy()
  const { getDocument } = await import('pdfjs-dist/legacy/build/pdf.mjs')
  const document = await getDocument({ data: new Uint8Array(await response.body()) }).promise
  const pages: string[] = []
  for (let number = 1; number <= document.numPages; number += 1) {
    const page = await document.getPage(number)
    const content = await page.getTextContent()
    pages.push(content.items.map((item) => ('str' in item ? item.str : '')).join(' '))
  }
  await document.destroy()
  return pages.join('\n')
}

async function acceptNextDialog(page: Page, value?: string) {
  const dialog = await page.waitForEvent('dialog')
  if (value === undefined) await dialog.dismiss()
  else await dialog.accept(value)
}

test.describe.serial('Cloverleaf authoring workflow', () => {
  test('initial load, edit, save, compile, preview, and editor keyboard behavior', async ({ page, request }) => {
    const consoleErrors: string[] = []
    const failedRequests: string[] = []
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    page.on('requestfailed', (failed) => failedRequests.push(`${failed.method()} ${failed.url()}`))

    const originalResponse = await request.get('/api/files/sections/introduction.tex')
    const original = ((await originalResponse.json()) as { content: string }).content

    try {
      await page.goto('/')
      await expect(page.getByLabel('Project files')).toBeVisible()
      await expect(page.getByLabel('Source editor')).toBeVisible()
      await expect(page.getByLabel('PDF preview')).toBeVisible()
      await expect(page.getByLabel('Codex assistant')).toBeVisible()
      await expect(page.locator('.project-path')).toContainText('main.tex')
      await expect(page.locator('.cm-content')).toContainText('documentclass')
      await expect(page.locator('.cm-lineNumbers')).toBeVisible()
      await expect(page.getByText('main.pdf', { exact: true })).toHaveCount(0)
      await expect(page.getByText('main.aux', { exact: true })).toHaveCount(0)
      await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
      await expect(page.getByTestId('pdf-page')).toHaveCount(3, { timeout: 30_000 })

      await page.screenshot({
        path: path.resolve(process.cwd(), '../screenshots/initial-desktop.png'),
        fullPage: true,
      })

      const divider = page.getByLabel('Resize editor and preview panels')
      const before = await divider.boundingBox()
      expect(before).not.toBeNull()
      await page.mouse.move(before!.x + 2, before!.y + before!.height / 2)
      await page.mouse.down()
      await page.mouse.move(before!.x + 70, before!.y + before!.height / 2)
      await page.mouse.up()
      const after = await divider.boundingBox()
      expect(Math.abs(after!.x - before!.x)).toBeGreaterThan(25)

      await page.getByLabel('Expand sections').click()
      await page.getByRole('button', { name: 'introduction.tex', exact: true }).click()
      await expect(page.locator('.project-path')).toContainText('sections/introduction.tex')
      const editor = page.locator('.cm-content')
      await expect(editor).toContainText('Edit this paragraph')

      await editor.press('ControlOrMeta+f')
      await expect(page.locator('.cm-search input').first()).toBeVisible()
      await page.locator('.cm-search input').first().fill('calm')
      await page.keyboard.press('Escape')

      const updated = original.replace(
        'Edit this paragraph',
        'Live browser validation confirms this paragraph',
      )
      const initialRevision = (await status(request)).revision
      await editor.press('ControlOrMeta+a')
      await page.keyboard.insertText(updated)
      await expect(page.locator('.save-state')).toHaveText('modified')

      await editor.press('ControlOrMeta+z')
      await expect(editor).toContainText('Edit this paragraph')
      await editor.press('ControlOrMeta+Shift+z')
      await expect(editor).toContainText('Live browser validation')

      await expect(page.locator('.save-state')).toHaveText('saved', { timeout: 10_000 })
      const revision = await waitForSuccessfulRevision(request, initialRevision)
      await expect.poll(() => pdfText(request), { timeout: 30_000 }).toContain(
        'Live browser validation confirms this paragraph',
      )

      const preview = page.locator('.pdf-scroll')
      await preview.evaluate((element) => {
        element.scrollTop = (element.scrollHeight - element.clientHeight) * 0.55
      })
      const ratioBefore = await preview.evaluate(
        (element) => element.scrollTop / (element.scrollHeight - element.clientHeight),
      )

      const finalText = updated.replace(
        'The interface favors',
        'The final queued build confirms that the interface favors',
      )
      await editor.press('ControlOrMeta+a')
      await page.keyboard.insertText(`${updated}\n\nRace pass one.`)
      await editor.press('ControlOrMeta+s')
      await editor.press('ControlOrMeta+a')
      await page.keyboard.insertText(finalText)
      await editor.press('ControlOrMeta+s')
      await waitForSuccessfulRevision(request, revision)
      await expect.poll(async () => {
        const response = await request.get('/api/files/sections/introduction.tex')
        return ((await response.json()) as { content: string }).content
      }).toBe(finalText)
      await expect.poll(() => pdfText(request), { timeout: 30_000 }).toContain(
        'final queued build confirms',
      )
      await expect(page.getByTestId('pdf-page')).toHaveCount(3)
      const ratioAfter = await preview.evaluate(
        (element) => element.scrollTop / (element.scrollHeight - element.clientHeight),
      )
      expect(Math.abs(ratioAfter - ratioBefore)).toBeLessThan(0.2)

      await page.screenshot({
        path: path.resolve(process.cwd(), '../screenshots/edited-desktop.png'),
        fullPage: true,
      })
      expect(consoleErrors).toEqual([])
      expect(failedRequests).toEqual([])
    } finally {
      await request.put('/api/files/sections/introduction.tex', {
        data: { path: 'sections/introduction.tex', content: original },
      })
      const beforeRestore = (await status(request)).revision
      await request.post('/api/compile')
      await waitForSuccessfulRevision(request, beforeRestore)
    }
  })

  test('file operations stay coherent and invalid actions are explained', async ({ page, request }) => {
    await request.delete('/api/files/live-check').catch(() => undefined)
    await page.goto('/')
    await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })

    try {
      const createFolderDialog = acceptNextDialog(page, 'live-check')
      await page.getByLabel('Create folder').click()
      await createFolderDialog
      await expect(page.getByRole('button', { name: 'live-check', exact: true })).toBeVisible()
      await page.getByLabel('Expand live-check').click()

      const cancelDialog = acceptNextDialog(page)
      await page.getByLabel('Create file').click()
      await cancelDialog
      await expect(page.getByText('Path already exists')).toHaveCount(0)

      const createFileDialog = acceptNextDialog(page, 'live-check/draft.tex')
      await page.getByLabel('Create file').click()
      await createFileDialog
      await expect(page.locator('.project-path')).toContainText('live-check/draft.tex')

      const editor = page.locator('.cm-content')
      await editor.press('ControlOrMeta+a')
      await page.keyboard.insertText('A temporary manuscript file.')
      await expect(page.locator('.save-state')).toHaveText('saved', { timeout: 10_000 })

      await page.getByRole('button', { name: 'draft.tex', exact: true }).hover()
      const renameDialog = acceptNextDialog(page, 'live-check/renamed.tex')
      await page.getByLabel('Rename live-check/draft.tex').click()
      await renameDialog
      await expect(page.locator('.project-path')).toContainText('live-check/renamed.tex')
      await expect(editor).toContainText('temporary manuscript')
      await expect(page.getByRole('button', { name: 'renamed.tex', exact: true })).toBeVisible()

      const duplicateDialog = acceptNextDialog(page, 'live-check')
      await page.getByLabel('Create folder').click()
      await duplicateDialog
      await expect(page.getByRole('alert')).toContainText('Path already exists')

      const invalidDialog = acceptNextDialog(page, '../outside.tex')
      await page.getByLabel('Create file').click()
      await invalidDialog
      await expect(page.getByRole('alert')).toContainText('relative and remain inside the project')

      await page.getByRole('button', { name: 'renamed.tex', exact: true }).hover()
      const deleteFileDialog = acceptNextDialog(page, 'confirm')
      await page.getByLabel('Delete live-check/renamed.tex').click()
      await deleteFileDialog
      await expect(page.locator('.project-path')).toContainText('—')
      await expect(page.locator('.cm-content')).toHaveCount(0)

      await page.getByRole('button', { name: 'live-check', exact: true }).hover()
      const deleteFolderDialog = acceptNextDialog(page, 'confirm')
      await page.getByLabel('Delete live-check').click()
      await deleteFolderDialog
      await expect(page.getByRole('button', { name: 'live-check', exact: true })).toHaveCount(0)
    } finally {
      await request.delete('/api/files/live-check').catch(() => undefined)
    }
  })

  test('large nested trees expand incrementally', async ({ page, request }) => {
    await request.post('/api/files', { data: { path: 'tree-scale/one/two', type: 'directory' } })
    await request.post('/api/files', {
      data: { path: 'tree-scale/one/two/deep.tex', type: 'file' },
    })
    try {
      await page.goto('/')
      await expect(page.getByRole('button', { name: 'tree-scale', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'one', exact: true })).toHaveCount(0)

      await page.getByLabel('Expand tree-scale').click()
      await expect(page.getByRole('button', { name: 'one', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'two', exact: true })).toHaveCount(0)

      await page.getByLabel('Expand one').click()
      await expect(page.getByRole('button', { name: 'two', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'deep.tex', exact: true })).toHaveCount(0)
    } finally {
      await request.delete('/api/files/tree-scale')
    }
  })

  test('editor text scrolls independently of the caret', async ({ page, request }) => {
    const filePath = 'editor-scroll-check.txt'
    const content = Array.from(
      { length: 300 },
      (_, index) => `Scrollable line ${String(index + 1).padStart(3, '0')}`,
    ).join('\n')
    await request.delete(`/api/files/${filePath}`).catch(() => undefined)
    await request.post('/api/files', {
      data: { path: filePath, type: 'file', content },
    })

    try {
      await page.goto('/')
      await page.getByRole('button', { name: filePath, exact: true }).click()
      await expect(page.locator('.cm-content')).toContainText('Scrollable line 001')

      const scroller = page.locator('.cm-scroller')
      const dimensions = await scroller.evaluate((element) => ({
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight,
      }))
      expect(dimensions.scrollHeight).toBeGreaterThan(dimensions.clientHeight)

      await page.locator('.cm-line').first().click()
      await scroller.hover()
      await page.mouse.wheel(0, 900)
      await expect.poll(() => scroller.evaluate((element) => element.scrollTop)).toBeGreaterThan(200)
    } finally {
      await request.delete(`/api/files/${filePath}`).catch(() => undefined)
    }
  })

  test('open files follow external changes without overwriting local edits', async ({ page, request }) => {
    const filePath = 'sections/introduction.tex'
    const original = (await (await request.get(`/api/files/${filePath}`)).json()) as {
      path: string
      content: string
    }
    const externallyUpdated = original.content.replace(
      'Edit this paragraph',
      'An external tool updated this paragraph',
    )
    const conflictingExternal = `${externallyUpdated}\n% external conflict proof\n`

    try {
      await page.goto('/')
      await page.getByLabel('Expand sections').click()
      await page.getByRole('button', { name: 'introduction.tex', exact: true }).click()
      const editor = page.locator('.cm-content')
      await expect(editor).toContainText('Edit this paragraph')

      const externalWrite = await request.put(`/api/files/${filePath}`, {
        data: { path: filePath, content: externallyUpdated },
      })
      expect(externalWrite.ok()).toBeTruthy()
      await expect(editor).toContainText('An external tool updated this paragraph', {
        timeout: 5_000,
      })

      await editor.press('ControlOrMeta+a')
      await page.keyboard.insertText(`${externallyUpdated}\n% unsaved Cloverleaf edit\n`)
      await expect(page.locator('.save-state')).toHaveText('modified')
      const conflictingWrite = await request.put(`/api/files/${filePath}`, {
        data: { path: filePath, content: conflictingExternal },
      })
      expect(conflictingWrite.ok()).toBeTruthy()

      await expect(page.getByRole('alert')).toContainText('changed on disk', { timeout: 5_000 })
      await expect(editor).toContainText('unsaved Cloverleaf edit')
      await expect.poll(async () => {
        const response = await request.get(`/api/files/${filePath}`)
        return ((await response.json()) as { content: string }).content
      }).toBe(conflictingExternal)
      await expect(page.locator('.save-state')).toHaveText('modified')
    } finally {
      await request.put(`/api/files/${filePath}`, {
        data: { path: filePath, content: original.content },
      })
      await request.post('/api/compile')
    }
  })

  test('projects can be picked, loaded, and bootstrapped', async ({ page, request }) => {
    const initial = (await (await request.get('/api/project')).json()) as {
      workspace: string
      name: string
      main_file: string
    }
    const temporary = await mkdtemp(path.join(os.tmpdir(), 'cloverleaf-project-'))
    const canonicalTemporary = await realpath(temporary)
    await mkdir(path.join(temporary, 'sections'))
    await mkdir(path.join(temporary, 'empty-project'))
    await writeFile(
      path.join(temporary, 'paper.tex'),
      '\\documentclass{article}\n\\begin{document}\nLoaded project proof.\n\\input{sections/note}\n\\end{document}\n',
      'utf8',
    )
    await writeFile(
      path.join(temporary, 'view.tex'),
      '\\documentclass{article}\n\\begin{document}\nAlternate view proof.\n\\end{document}\n',
      'utf8',
    )
    await writeFile(path.join(temporary, 'sections', 'note.tex'), 'A second source file.\n', 'utf8')

    try {
      await page.goto('/')
      await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })

      await page.getByLabel('Load project').click()
      let picker = page.getByRole('dialog', { name: 'Load project' })
      await expect(picker).toBeVisible()
      await expect(picker.locator('input')).toHaveCount(0)
      await picker.getByRole('button', { name: 'Cancel' }).click()
      await expect(picker).toHaveCount(0)
      await expect(page.locator('.project-path')).toContainText(
        `${initial.name} / ${initial.main_file}`,
      )

      await page.getByLabel('Load project').click()
      picker = page.getByRole('dialog', { name: 'Load project' })
      await expect(picker).toBeVisible()
      await page.screenshot({
        path: path.resolve(process.cwd(), '../screenshots/project-picker.png'),
        fullPage: true,
      })
      await picker.getByRole('button', { name: 'Computer' }).click()
      const filesystemRoot = path.parse(canonicalTemporary).root
      const segments = path.relative(filesystemRoot, canonicalTemporary).split(path.sep).filter(Boolean)
      for (const segment of segments) {
        await picker.getByRole('button', { name: `Open folder ${segment}`, exact: true }).click()
      }
      await expect(picker.getByLabel('Compilation root')).toHaveValue('paper.tex')
      await picker.getByRole('button', { name: 'Open project' }).click()
      await expect(picker).toHaveCount(0)
      await expect(page.locator('.project-path')).toContainText(
        `${path.basename(temporary)} / paper.tex`,
      )
      await expect(page.locator('.cm-content')).toContainText('Loaded project proof')
      await expect(page.getByRole('button', { name: 'note.tex', exact: true })).toHaveCount(0)
      await page.getByLabel('Expand sections').click()
      await expect(page.getByRole('button', { name: 'note.tex', exact: true })).toBeVisible()
      await expect(page.getByText('paper.pdf', { exact: true })).toHaveCount(0)
      await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
      await expect(page.getByTestId('pdf-page')).toHaveCount(1, { timeout: 30_000 })
      await expect(page.locator('.preview-pages')).toHaveText('1 page')
      await expect.poll(() => pdfText(request), { timeout: 30_000 }).toContain('Loaded project proof')

      const loaded = await (await request.get('/api/project')).json()
      expect(loaded).toMatchObject({ workspace: canonicalTemporary, main_file: 'paper.tex' })

      await page.getByRole('button', { name: 'view.tex', exact: true }).click()
      await expect(page.getByLabel('Compile view.tex as the current view')).toBeVisible()
      await page.evaluate(() => {
        const browserWindow = window as typeof window & {
          __savedPdf?: { name: string; size: number; type: string; closed: boolean }
          showSaveFilePicker?: (options: { suggestedName: string }) => Promise<{
            createWritable: () => Promise<{
              write: (data: Blob) => Promise<void>
              close: () => Promise<void>
            }>
          }>
        }
        Object.defineProperty(browserWindow, 'showSaveFilePicker', {
          configurable: true,
          value: async (options: { suggestedName: string }) => ({
            createWritable: async () => ({
              write: async (data: Blob) => {
                browserWindow.__savedPdf = {
                  name: options.suggestedName,
                  size: data.size,
                  type: data.type,
                  closed: false,
                }
              },
              close: async () => {
                if (browserWindow.__savedPdf) browserWindow.__savedPdf.closed = true
              },
            }),
          }),
        })
      })
      await page.getByLabel('Compile view.tex as the current view').click()
      await expect(page.locator('.compiler-root')).toContainText('view.tex')
      await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
      await expect(page.getByTestId('pdf-page')).toHaveCount(1, { timeout: 30_000 })
      await expect.poll(() => pdfText(request), { timeout: 30_000 }).toContain('Alternate view proof')
      expect(await (await request.get('/api/project')).json()).toMatchObject({
        workspace: canonicalTemporary,
        main_file: 'view.tex',
      })

      await page.getByLabel('Save PDF as view.pdf').click()
      await expect.poll(() => page.evaluate(() => {
        const saved = (window as typeof window & {
          __savedPdf?: { name: string; size: number; type: string; closed: boolean }
        }).__savedPdf
        return saved ? { ...saved, hasContent: saved.size > 100 } : null
      })).toEqual({
        name: 'view.pdf',
        size: expect.any(Number),
        type: 'application/pdf',
        closed: true,
        hasContent: true,
      })

      await page.screenshot({
        path: path.resolve(process.cwd(), '../screenshots/alternate-view.png'),
        fullPage: true,
      })

      await page.screenshot({
        path: path.resolve(process.cwd(), '../screenshots/loaded-project.png'),
        fullPage: true,
      })

      await page.getByLabel('Load project').click()
      picker = page.getByRole('dialog', { name: 'Load project' })
      await picker.getByRole('button', { name: 'Open folder empty-project' }).click()
      await expect(picker.getByLabel('Compilation root')).toHaveValue('main.tex')
      await expect(picker.getByRole('option')).toHaveText('main.tex (will be created)')
      await picker.getByRole('button', { name: 'Open project' }).click()
      await expect(page.locator('.project-path')).toContainText('empty-project / main.tex')
      await expect(page.locator('.cm-content')).toContainText('Start writing here.')
      await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
      await expect(page.getByTestId('pdf-page')).toHaveCount(1, { timeout: 30_000 })
      await expect.poll(() => pdfText(request), { timeout: 30_000 }).toContain('Start writing here.')
      expect(
        ((await (await request.get('/api/files/main.tex')).json()) as { content: string }).content,
      ).toContain('\\documentclass{article}')

      await page.screenshot({
        path: path.resolve(process.cwd(), '../screenshots/created-main-project.png'),
        fullPage: true,
      })
    } finally {
      const restored = await request.post('/api/project/load', {
        data: { workspace: initial.workspace, main_file: initial.main_file },
      })
      expect(restored.ok()).toBeTruthy()
      await request.post('/api/compile')
      await expect.poll(async () => (await status(request)).state, { timeout: 30_000 }).toBe('success')
      await rm(temporary, { recursive: true, force: true })
    }
  })

  test('new projects can use a selected root or create a root directory', async ({ page, request }) => {
    const initial = (await (await request.get('/api/project')).json()) as {
      workspace: string
      name: string
      main_file: string
    }
    const temporary = await mkdtemp(
      path.join(path.dirname(initial.workspace), 'cloverleaf-new-project-'),
    )
    const selectedRoot = path.join(temporary, 'selected-root')
    await mkdir(selectedRoot)

    try {
      await page.goto('/')
      await page.getByLabel('New project').click()
      let picker = page.getByRole('dialog', { name: 'New project' })
      await expect(picker).toBeVisible()
      await expect(picker.getByLabel('New folder name')).toHaveValue('')
      await picker.getByRole('button', { name: 'Up' }).click()
      await picker.getByRole('button', {
        name: `Open folder ${path.basename(temporary)}`,
        exact: true,
      }).click()
      await picker.getByRole('button', { name: 'Open folder selected-root', exact: true }).click()
      await expect(picker.getByRole('button', { name: 'Create project here' })).toBeEnabled()

      await page.screenshot({
        path: path.resolve(process.cwd(), '../screenshots/new-project-picker.png'),
        fullPage: true,
      })
      await picker.getByRole('button', { name: 'Create project here' }).click()

      await expect(picker).toHaveCount(0)
      await expect(page.locator('.project-path')).toContainText('selected-root / main.tex')
      await expect(page.locator('.cm-content')).toContainText('Start writing here.')
      await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
      expect(
        ((await (await request.get('/api/project')).json()) as { workspace: string }).workspace,
      ).toBe(await realpath(selectedRoot))
      expect(
        (await (await request.get('/api/project/git')).json()) as {
          initialized: boolean
          head: string | null
          last_error: string | null
        },
      ).toMatchObject({ initialized: true, head: expect.any(String), last_error: null })
      await expect.poll(async () => realpath(path.join(selectedRoot, '.git'))).toBe(
        path.join(await realpath(selectedRoot), '.git'),
      )

      await page.getByLabel('New project').click()
      picker = page.getByRole('dialog', { name: 'New project' })
      await picker.getByRole('button', { name: 'Up' }).click()
      await picker.getByLabel('New folder name').fill('created-root')
      await expect(picker.getByText('create this folder and initialize main.tex')).toBeVisible()
      await picker.getByRole('button', { name: 'Create project', exact: true }).click()

      const createdRoot = path.join(temporary, 'created-root')
      await expect(page.locator('.project-path')).toContainText('created-root / main.tex')
      await expect(page.locator('.cm-content')).toContainText('Start writing here.')
      await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
      await expect(page.getByTestId('pdf-page')).toHaveCount(1, { timeout: 30_000 })
      expect(
        ((await (await request.get('/api/project')).json()) as { workspace: string }).workspace,
      ).toBe(await realpath(createdRoot))
      expect(
        ((await (await request.get('/api/files/main.tex')).json()) as { content: string }).content,
      ).toContain('Start writing here.')
      expect(
        (await (await request.get('/api/project/git')).json()) as {
          initialized: boolean
          head: string | null
          last_error: string | null
        },
      ).toMatchObject({ initialized: true, head: expect.any(String), last_error: null })
      await expect.poll(async () => realpath(path.join(createdRoot, '.git'))).toBe(
        path.join(await realpath(createdRoot), '.git'),
      )
    } finally {
      const restored = await request.post('/api/project/load', {
        data: { workspace: initial.workspace, main_file: initial.main_file },
      })
      expect(restored.ok()).toBeTruthy()
      await request.post('/api/compile')
      await expect.poll(async () => (await status(request)).state, { timeout: 30_000 }).toBe('success')
      await rm(temporary, { recursive: true, force: true })
    }
  })
})
