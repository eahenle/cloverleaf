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

test('assistant keeps the newest response and edit controls in view', async ({ page }) => {
  let requestContext: Record<string, unknown> | undefined
  await page.route('**/api/assistant/chat', (route) => {
    requestContext = (route.request().postDataJSON() as { context: Record<string, unknown> }).context
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        message: Array.from({ length: 60 }, (_, index) => `Explanation line ${index + 1}`).join('\n'),
        proposed_edits: [{
          path: 'main.tex',
          content: '\\documentclass{article}',
          summary: 'A reviewable replacement',
          replacements: [{ old_text: 'Old title', new_text: 'New title' }],
        }],
      }),
    })
  })

  await page.goto('/')
  await page.getByLabel('Message Codex').fill('Explain this and suggest an edit')
  await page.getByLabel('Send message').click()
  await expect(page.getByRole('button', { name: 'Apply' })).toBeInViewport()
  await expect(page.getByRole('button', { name: 'Dismiss' })).toBeInViewport()
  expect(await page.locator('.messages').evaluate((element) =>
    Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop),
  )).toBeLessThanOrEqual(1)
  await page.getByText('Review changes').click()
  await expect(page.locator('.edit-review')).toContainText('Before')
  await expect(page.locator('.edit-review')).toContainText('Old title')
  await expect(page.locator('.edit-review')).toContainText('After')
  await expect(page.locator('.edit-review')).toContainText('New title')
  await expect(page.getByRole('button', { name: 'Apply' })).toBeInViewport()
  await expect(page.getByRole('button', { name: 'Dismiss' })).toBeInViewport()
  expect(requestContext).not.toHaveProperty('project_tree')
  expect(requestContext).toMatchObject({
    main_file: 'main.tex',
    open_file: 'main.tex',
    compile_state: expect.any(String),
  })
})

test('a confirmed Codex edit creates the reviewed file', async ({ page, request }) => {
  const filePath = 'codex-review-note.txt'
  await request.delete(`/api/files/${filePath}`).catch(() => undefined)
  await page.route('**/api/assistant/chat', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        message: 'Created a concrete project note for review.',
        proposed_edits: [{
          path: filePath,
          content: 'A reviewed Codex file edit.\n',
          summary: 'Create the requested project note',
          version: null,
          is_new: true,
        }],
      }),
    }),
  )

  try {
    await page.goto('/')
    await page.getByLabel('Message Codex').fill('Create a project note')
    await page.getByLabel('Send message').click()
    await expect(page.getByText('Review new file')).toBeVisible()
    expect((await request.get(`/api/files/${filePath}`)).status()).toBe(404)

    page.once('dialog', (dialog) => dialog.accept())
    await page.getByRole('button', { name: 'Apply' }).click()
    await expect(page.locator('.project-path')).toContainText(filePath)
    await expect(page.locator('.cm-content')).toContainText('reviewed Codex file edit')
    expect(
      ((await (await request.get(`/api/files/${filePath}`)).json()) as { content: string }).content,
    ).toBe('A reviewed Codex file edit.\n')
  } finally {
    await request.delete(`/api/files/${filePath}`).catch(() => undefined)
  }
})

test('a multi-file Codex response applies together and compiles once', async ({ page, request }) => {
  const paths = ['codex-batch-a.txt', 'codex-batch-b.txt']
  for (const path of paths) await request.delete(`/api/files/${path}`).catch(() => undefined)
  let compileRequests = 0
  page.on('request', (outbound) => {
    if (outbound.method() === 'POST' && outbound.url().endsWith('/api/compile')) {
      compileRequests += 1
    }
  })
  await page.route('**/api/assistant/chat', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        message: 'Prepared two coordinated project notes.',
        proposed_edits: paths.map((path, index) => ({
          path,
          content: `Batch file ${index + 1}.\n`,
          summary: `Create batch file ${index + 1}`,
          version: null,
          is_new: true,
        })),
      }),
    }),
  )

  try {
    await page.goto('/')
    await page.getByLabel('Message Codex').fill('Create both project notes')
    await page.getByLabel('Send message').click()
    const applyAll = page.getByRole('button', { name: 'Apply all 2 edits' })
    await expect(applyAll).toBeVisible()
    const beforeApply = compileRequests

    page.once('dialog', (dialog) => dialog.accept())
    await applyAll.click()
    await expect(applyAll).toBeHidden()
    await expect(page.locator('.project-path')).toContainText(paths[0])
    await expect.poll(() => compileRequests).toBe(beforeApply + 1)
    for (const [index, path] of paths.entries()) {
      const response = await request.get(`/api/files/${path}`)
      expect(response.ok()).toBe(true)
      expect(((await response.json()) as { content: string }).content)
        .toBe(`Batch file ${index + 1}.\n`)
    }
  } finally {
    for (const path of paths) await request.delete(`/api/files/${path}`).catch(() => undefined)
  }
})

test('assistant shows elapsed time and live runtime health', async ({ page }) => {
  await page.routeWebSocket('**/api/assistant/progress/*', (socket) => {
    socket.send(JSON.stringify({
      phase: 'inspecting',
      message: 'Inspecting project files in the read-only workspace…',
      activity_count: 4,
      heartbeat: false,
    }))
    setTimeout(() => socket.send(JSON.stringify({
      phase: 'inspecting',
      message: 'Inspecting project files in the read-only workspace…',
      activity_count: 4,
      heartbeat: true,
    })), 650)
  })
  await page.route('**/api/assistant/chat', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_400))
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ message: 'Finished after inspection.', proposed_edits: [] }),
    })
  })

  await page.goto('/')
  await page.getByLabel('Message Codex').fill('Inspect the project')
  await page.getByLabel('Send message').click()
  const activity = page.getByLabel('Codex activity')
  await expect(activity).toContainText('Inspecting project files')
  await expect(activity).toContainText('stream live')
  await expect(activity).toContainText('4 runtime updates')
  await expect(activity).toContainText(/1s elapsed/)
  await expect(page.locator('.message.assistant').last()).toContainText('Finished after inspection.')
})

test('terminal tab gates and confirms launcher shutdown', async ({ page }) => {
  let shutdownRequests = 0
  await page.route('**/api/runtime', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ managed: true, log_available: false, shutdown_available: true }),
    }),
  )
  await page.route('**/api/runtime/shutdown', (route) => {
    shutdownRequests += 1
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, message: 'Server shutdown requested' }),
    })
  })

  await page.goto('/')
  await page.getByRole('tab', { name: 'Terminal' }).click()
  await expect(page.getByLabel('Server terminal')).toBeVisible()
  await expect(page.getByText('backend + frontend stdout/stderr')).toBeVisible()
  const shutdown = page.getByRole('button', { name: 'Shut down server' })
  await expect(shutdown).toBeEnabled()

  page.once('dialog', (dialog) => void dialog.dismiss())
  await shutdown.click()
  expect(shutdownRequests).toBe(0)

  page.once('dialog', (dialog) => void dialog.accept())
  await shutdown.click()
  await expect(page.getByText('Server shutdown requested. This page will disconnect.')).toBeVisible()
  expect(shutdownRequests).toBe(1)
})
