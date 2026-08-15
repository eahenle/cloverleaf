import { expect, test, type APIRequestContext } from '@playwright/test'
import path from 'node:path'

type Status = {
  state: string
  revision: number
  diagnostics: Array<{ message: string; file?: string | null; line?: number | null }>
}

async function compileStatus(request: APIRequestContext): Promise<Status> {
  return (await (await request.get('/api/compile/status')).json()) as Status
}

async function waitForState(request: APIRequestContext, state: string): Promise<Status> {
  await expect.poll(async () => (await compileStatus(request)).state, { timeout: 30_000 }).toBe(state)
  return compileStatus(request)
}

async function replaceEditor(page: import('@playwright/test').Page, content: string) {
  const editor = page.locator('.cm-content')
  await editor.press('ControlOrMeta+a')
  await page.keyboard.insertText(content)
  await editor.press('ControlOrMeta+s')
}

test('compiler failures retain the last preview and recover cleanly', async ({ page, request }) => {
  const sourceResponse = await request.get('/api/files/sections/introduction.tex')
  const original = ((await sourceResponse.json()) as { content: string }).content
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })

  try {
    await page.goto('/')
    await waitForState(request, 'success')
    await expect(page.getByTestId('pdf-page')).toHaveCount(3, { timeout: 30_000 })
    await page.getByRole('button', { name: 'introduction.tex', exact: true }).click()

    await replaceEditor(page, '\\section{Welcome}\n\n\\cloverleafUndefinedCommand\n')
    let failed = await waitForState(request, 'error')
    expect(failed.diagnostics.some((item) => /undefined control sequence/i.test(item.message))).toBeTruthy()
    expect(failed.diagnostics.some((item) => item.file === 'sections/introduction.tex' && item.line === 3)).toBeTruthy()
    await expect(page.locator('.compiler-state')).toHaveClass(/error/)
    await expect(page.locator('.diagnostic').first()).toContainText(/Undefined control sequence/i)
    await expect(page.getByTestId('pdf-page')).toHaveCount(3)
    await expect(page.locator('.preview-badge.error')).toContainText('last successful build')
    await expect(page.locator('.build-log')).not.toHaveAttribute('open', '')
    expect((await request.get('/api/health')).ok()).toBeTruthy()

    await page.screenshot({
      path: path.resolve(process.cwd(), '../screenshots/compiler-error.png'),
      fullPage: true,
    })

    await replaceEditor(page, original)
    await waitForState(request, 'success')
    await expect(page.locator('.preview-badge.error')).toHaveCount(0)

    await replaceEditor(page, '\\section{Welcome\n\nThis section title is missing its closing brace.\n')
    failed = await waitForState(request, 'error')
    expect(failed.diagnostics.length).toBeGreaterThan(0)
    expect(
      failed.diagnostics.some((item) => /file ended|runaway|emergency|missing/i.test(item.message)),
    ).toBeTruthy()
    await expect(page.locator('.diagnostic').first()).toBeVisible()
    await expect(page.getByTestId('pdf-page')).toHaveCount(3)
    expect((await request.get('/api/health')).ok()).toBeTruthy()

    await replaceEditor(page, original)
    await waitForState(request, 'success')
    await expect(page.getByTestId('pdf-page')).toHaveCount(3)
    expect(consoleErrors).toEqual([])
  } finally {
    await request.put('/api/files/sections/introduction.tex', {
      data: { path: 'sections/introduction.tex', content: original },
    })
    await request.post('/api/compile')
    await waitForState(request, 'success')
  }
})
