import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import path from 'node:path'

test.skip(process.env.CLOVERLEAF_LIVE_CODEX !== '1', 'Set CLOVERLEAF_LIVE_CODEX=1 to use authenticated Codex')

async function ask(page: Page, prompt: string): Promise<string> {
  const responses = page.locator('.message.assistant')
  const before = await responses.count()
  await page.getByLabel('Message Codex').fill(prompt)
  await page.getByLabel('Send message').click()
  await expect(responses).toHaveCount(before + 1, { timeout: 120_000 })
  return (await responses.last().innerText()).trim()
}

async function waitForCompileState(request: APIRequestContext, state: string) {
  await expect.poll(async () => {
    const response = await request.get('/api/compile/status')
    return ((await response.json()) as { state: string }).state
  }, { timeout: 30_000 }).toBe(state)
}

async function replaceEditor(page: Page, content: string) {
  const editor = page.locator('.cm-content')
  await editor.press('ControlOrMeta+a')
  await page.keyboard.insertText(content)
  await editor.press('ControlOrMeta+s')
}

test('authenticated Codex uses manuscript context and gates proposed edits', async ({ page, request }) => {
  const originalResponse = await request.get('/api/files/sections/introduction.tex')
  const original = ((await originalResponse.json()) as { content: string }).content
  const assistantPayloads: unknown[] = []
  page.on('request', (outbound) => {
    if (outbound.url().endsWith('/api/assistant/chat') && outbound.postDataJSON()) {
      assistantPayloads.push(outbound.postDataJSON())
    }
  })

  try {
    await page.goto('/')
    await waitForCompileState(request, 'success')

    const general = await ask(page, 'In one concise sentence, what is this manuscript about?')
    expect(general).toMatch(/Cloverleaf|writing|authoring|local/i)

    const openFile = await ask(
      page,
      'Using the currently open file, what section title follows the second \\newpage? Reply with only the title.',
    )
    expect(openFile).toMatch(/Local by design/i)

    const editor = page.locator('.cm-content')
    await editor.press('ControlOrMeta+f')
    await page.locator('.cm-search input').first().fill('Local by design')
    await page.locator('.cm-search input').first().press('Enter')
    await page.keyboard.press('Escape')
    await editor.press('ControlOrMeta+a')
    const selected = await ask(
      page,
      'From the selected editor text, quote the section title that contains the word Local and nothing else.',
    )
    expect(selected).toContain('Local by design')

    await page.getByRole('button', { name: 'introduction.tex', exact: true }).click()
    await expect(page.locator('.project-path')).toContainText('sections/introduction.tex')
    await expect(page.locator('.cm-content')).toContainText('Overleaf-inspired')
    await replaceEditor(page, '\\section{Welcome}\n\n\\cloverleafUndefinedCommand\n')
    await waitForCompileState(request, 'error')
    const explanation = await ask(
      page,
      'Explain the active compiler error in one sentence and identify the offending command.',
    )
    expect(explanation).toMatch(/undefined|cloverleafUndefinedCommand/i)

    await replaceEditor(page, original)
    await waitForCompileState(request, 'success')
    const beforeProposal = ((await (await request.get('/api/files/sections/introduction.tex')).json()) as { content: string }).content

    await ask(
      page,
      "Propose a complete-file edit for sections/introduction.tex that preserves everything except replacing 'Overleaf-inspired' with 'focused local'. Return it using Cloverleaf's required cloverleaf-edits block.",
    )
    await expect(page.locator('.proposed-edit').last()).toContainText('sections/introduction.tex')
    const afterConversation = ((await (await request.get('/api/files/sections/introduction.tex')).json()) as { content: string }).content
    expect(afterConversation).toBe(beforeProposal)

    await page.getByRole('button', { name: 'Dismiss' }).last().click()
    await expect(page.locator('.proposed-edit')).toHaveCount(0)
    expect(((await (await request.get('/api/files/sections/introduction.tex')).json()) as { content: string }).content).toBe(beforeProposal)

    await ask(
      page,
      "Propose that same complete-file edit for sections/introduction.tex again using the cloverleaf-edits block.",
    )
    const card = page.locator('.proposed-edit').last()
    await expect(card).toBeVisible()

    await page.screenshot({
      path: path.resolve(process.cwd(), '../screenshots/live-codex-proposal.png'),
      fullPage: true,
    })

    page.once('dialog', (dialog) => dialog.dismiss())
    await card.getByRole('button', { name: 'Apply' }).click()
    await expect(card).toBeVisible()
    expect(((await (await request.get('/api/files/sections/introduction.tex')).json()) as { content: string }).content).toBe(beforeProposal)

    page.once('dialog', (dialog) => dialog.accept())
    await card.getByRole('button', { name: 'Apply' }).click()
    await expect.poll(async () => {
      const response = await request.get('/api/files/sections/introduction.tex')
      return ((await response.json()) as { content: string }).content
    }).toContain('focused local')
    await expect(page.locator('.project-path')).toContainText('sections/introduction.tex')
    await waitForCompileState(request, 'success')
    await expect(page.locator('.topbar-build')).toHaveClass(/success/)
    await expect(page.getByTestId('pdf-page')).toHaveCount(3)
    await expect(page.locator('.proposed-edit')).toHaveCount(0)

    await page.screenshot({
      path: path.resolve(process.cwd(), '../screenshots/live-codex-applied.png'),
      fullPage: true,
    })

    expect(assistantPayloads.length).toBeGreaterThanOrEqual(6)
    for (const payload of assistantPayloads) {
      const encoded = JSON.stringify(payload)
      expect(encoded).not.toMatch(/AI_API_KEY|Authorization|Bearer |access_token|refresh_token/i)
      expect(Object.keys(payload as Record<string, unknown>).sort()).toEqual(['context', 'messages'])
    }
  } finally {
    await request.put('/api/files/sections/introduction.tex', {
      data: { path: 'sections/introduction.tex', content: original },
    })
    await request.post('/api/compile')
    await waitForCompileState(request, 'success')
  }
})
