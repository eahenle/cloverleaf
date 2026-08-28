import { expect, test, type APIRequestContext } from '@playwright/test'
import path from 'node:path'
import { clearAssistantHistory } from './history'

test.beforeEach(async ({ request }) => clearAssistantHistory(request))

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
  let repairRequest: {
    messages: Array<{ role: string; content: string }>
    context: {
      compile_state: string
      diagnostics: Status['diagnostics']
      compile_log: string
    }
  } | undefined
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  await page.route('**/api/assistant/chat', (route) => {
    repairRequest = route.request().postDataJSON() as typeof repairRequest
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        message: 'I found the undefined command and prepared a minimal repair.',
        proposed_edits: [{
          path: 'sections/introduction.tex',
          content: '\\section{Welcome}\n\n',
          summary: 'Remove the undefined command',
          replacements: [{ old_text: '\\cloverleafUndefinedCommand\n', new_text: '' }],
          version: 'test-version',
          is_new: false,
        }],
      }),
    })
  })

  try {
    await page.goto('/')
    await waitForState(request, 'success')
    await expect(page.getByTestId('pdf-page')).toHaveCount(3, { timeout: 30_000 })
    await page.getByLabel('Expand sections').click()
    await page.getByRole('button', { name: 'introduction.tex', exact: true }).click()
    await expect(page.locator('.project-path')).toContainText('sections/introduction.tex')
    await expect(page.locator('.cm-content')).toContainText('Overleaf-inspired')

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

    await page.getByRole('tab', { name: 'Terminal' }).click()
    await page.getByLabel('Fix compilation with Codex').click()
    await expect(page.getByRole('tab', { name: 'Codex' })).toHaveAttribute('aria-selected', 'true')
    await expect(page.locator('.message.user').last()).toContainText('Fix the current LaTeX compilation failure')
    await expect(page.locator('.message.assistant').last()).toContainText('prepared a minimal repair')
    await expect(page.locator('.proposed-edit').last()).toContainText('Remove the undefined command')
    expect(repairRequest?.messages.at(-1)?.content).toContain('smallest safe edit')
    expect(repairRequest?.context).toMatchObject({
      compile_state: 'error',
      diagnostics: expect.arrayContaining([
        expect.objectContaining({ message: expect.stringMatching(/undefined control sequence/i) }),
      ]),
    })
    expect(repairRequest?.context.compile_log).toMatch(/cloverleafUndefinedCommand/)

    await page.screenshot({
      path: path.resolve(process.cwd(), '../screenshots/compiler-error.png'),
      fullPage: true,
    })

    await replaceEditor(page, original)
    await waitForState(request, 'success')
    await expect(page.locator('.preview-badge.error')).toHaveCount(0)
    await expect(page.getByLabel('Fix compilation with Codex')).toHaveCount(0)

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
