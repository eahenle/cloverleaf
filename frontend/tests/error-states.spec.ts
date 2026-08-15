import { expect, test } from '@playwright/test'

const missingCompiler = {
  state: 'error',
  diagnostics: [{ severity: 'error', message: 'latexmk is not installed or is not on PATH' }],
  log_tail: '',
  revision: 0,
}

test('missing latexmk and assistant failures are useful, contained states', async ({ page }) => {
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  await page.route('**/api/compile', (route) =>
    route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify(missingCompiler) }),
  )
  await page.route('**/api/compile/status', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(missingCompiler) }),
  )
  await page.route('**/api/assistant/chat', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Codex is unavailable. Run codex login and try again.' }),
    }),
  )

  await page.goto('/')
  await expect(page.locator('.compiler-state')).toHaveClass(/error/)
  await expect(page.locator('.diagnostic')).toContainText('latexmk is not installed')
  await expect(page.locator('.preview-message')).toContainText('No successful PDF build')

  await page.getByLabel('Message Codex').fill('What is this manuscript about?')
  await page.getByLabel('Send message').click()
  await expect(page.locator('.message.assistant').last()).toContainText('Codex is unavailable')
  await expect(page.getByRole('alert')).toContainText('Codex is unavailable')
  await expect(page.getByLabel('Source editor')).toBeVisible()
  expect(consoleErrors).toHaveLength(1)
  expect(consoleErrors[0]).toContain('503')
})

test('a missing PDF response produces a clear preview error', async ({ page }) => {
  const success = { state: 'success', diagnostics: [], log_tail: '', revision: 999 }
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  await page.route('**/api/compile', (route) =>
    route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify(success) }),
  )
  await page.route('**/api/compile/status', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(success) }),
  )
  await page.route('**/api/pdf?revision=999', (route) =>
    route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'No successful PDF build is available yet' }),
    }),
  )

  await page.goto('/')
  await expect(page.locator('.preview-message.error')).toContainText(
    'Could not render the PDF: No successful PDF build is available yet',
  )
  expect(consoleErrors).toHaveLength(1)
  expect(consoleErrors[0]).toContain('404')
})
