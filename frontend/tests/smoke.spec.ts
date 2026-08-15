import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import path from 'node:path'

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
      await expect(page.getByRole('alert')).toContainText('visible relative path')

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
})
