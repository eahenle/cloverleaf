import { expect, test } from '@playwright/test'
import { clearAssistantHistory } from './history'

test.beforeEach(async ({ request }) => clearAssistantHistory(request))

test('line-number clicks pin removable context and queued prompts retain their own line', async ({ page }) => {
  const contexts: Array<Record<string, unknown>> = []
  let releaseFirst!: () => void
  const firstTurn = new Promise<void>((resolve) => { releaseFirst = resolve })
  await page.route('**/api/assistant/chat', async (route) => {
    contexts.push(route.request().postDataJSON().context)
    if (contexts.length === 1) await firstTurn
    await route.fulfill({ json: { message: 'Inspected line', proposed_edits: [] } })
  })
  await page.goto('/')
  const line = (number: number) => page.locator('.cm-lineNumbers .cm-gutterElement').filter({ hasText: new RegExp(`^${number}$`) })
  await line(1).click()
  await expect(page.getByLabel('Codex line context')).toContainText('main.tex:1')
  await page.getByLabel('Message Codex').fill('Inspect the first line')
  await page.getByLabel('Send message').click()
  await expect(page.getByLabel('Codex line context')).toHaveCount(0)
  await expect.poll(() => contexts.length).toBe(1)
  expect(contexts[0].selected_line).toEqual({ path: 'main.tex', line: 1, text: '\\documentclass[11pt]{article}' })
  await line(3).click()
  await page.getByLabel('Message Codex').fill('Inspect the title')
  await page.getByLabel('Send message').click()
  await expect(page.getByLabel('Codex line context')).toHaveCount(0)
  await expect(page.locator('.queued')).toContainText('Inspect the title')
  await line(4).click()
  releaseFirst()
  await expect.poll(() => contexts.length).toBe(2)
  expect(contexts[1].selected_line).toEqual({ path: 'main.tex', line: 3, text: '\\title{Cloverleaf}' })
  await page.getByLabel('Clear line context').click()
  await expect(page.getByLabel('Codex line context')).toHaveCount(0)
  await page.getByLabel('Message Codex').fill('No line context')
  await page.getByLabel('Send message').click()
  await expect(page.getByLabel('Codex line context')).toHaveCount(0)
  await expect.poll(() => contexts.length).toBe(3)
  expect(contexts[2].selected_line).toBeNull()
  expect(contexts[2].selected_text).toBeNull()
  await line(1).click()
  await page.getByLabel('Expand sections').click()
  await page.getByRole('button', { name: 'introduction.tex', exact: true }).click()
  await expect(page.getByLabel('Codex line context')).toHaveCount(0)
})
