import { expect, test } from '@playwright/test'
import { clearAssistantHistory } from './history'

test.beforeEach(async ({ request }) => clearAssistantHistory(request))

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
        warnings: ['Cloverleaf rejected an edit outside the active project: "../outside.tex".'],
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
  await expect(page.locator('.assistant-warning')).toContainText('rejected an edit outside')
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

test('initial project hydration preserves a draft typed before the project loads', async ({ page }) => {
  let releaseProject!: () => void
  const projectLoaded = new Promise<void>((resolve) => {
    releaseProject = resolve
  })
  const requests: string[] = []

  await page.route('**/api/project', async (route) => {
    const response = await route.fetch()
    await projectLoaded
    await route.fulfill({ response })
  })
  await page.route('**/api/assistant/chat', async (route) => {
    const body = route.request().postDataJSON() as {
      messages: Array<{ content: string }>
    }
    requests.push(body.messages.at(-1)?.content ?? '')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ message: 'Draft received.', proposed_edits: [] }),
    })
  })

  await page.goto('/')
  const input = page.getByLabel('Message Codex')
  await input.fill('Draft typed while project loads')
  await expect(input).toHaveValue('Draft typed while project loads')

  releaseProject()

  await expect(page.locator('.project-path')).toContainText('main.tex')
  await expect(input).toHaveValue('Draft typed while project loads')
  await input.press('Enter')
  await expect.poll(() => requests).toEqual(['Draft typed while project loads'])
  await expect(page.getByText('Draft received.')).toBeVisible()
})

test('Codex input recalls and repeats earlier messages with arrow keys', async ({ page }) => {
  const requests: string[] = []
  await page.route('**/api/assistant/chat', (route) => {
    const body = route.request().postDataJSON() as { messages: Array<{ content: string }> }
    requests.push(body.messages.at(-1)?.content ?? '')
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ message: 'Done.', proposed_edits: [], warnings: [] }),
    })
  })

  await page.goto('/')
  const input = page.getByLabel('Message Codex')
  await input.fill('First request')
  await input.press('Enter')
  await expect.poll(() => requests).toEqual(['First request'])
  await expect(page.locator('.message.assistant')).toHaveCount(1)
  await input.fill('Second request')
  await input.press('Enter')
  await expect.poll(() => requests).toEqual(['First request', 'Second request'])
  await expect(page.locator('.message.assistant')).toHaveCount(2)

  await input.press('ArrowUp')
  await expect(input).toHaveValue('Second request')
  await input.press('ArrowUp')
  await expect(input).toHaveValue('First request')
  await input.press('ArrowDown')
  await expect(input).toHaveValue('Second request')
  await input.press('Enter')
  await expect.poll(() => requests).toEqual([
    'First request',
    'Second request',
    'Second request',
  ])
  await expect(page.locator('.message.assistant')).toHaveCount(3)
})

test('Codex inputs submitted during a turn queue as ordered future rounds', async ({ page }) => {
  const requests: Array<Array<{ role: string; content: string }>> = []
  let releaseFirst!: () => void
  const firstResponse = new Promise<void>((resolve) => {
    releaseFirst = resolve
  })
  await page.route('**/api/assistant/chat', async (route) => {
    const body = route.request().postDataJSON() as {
      messages: Array<{ role: string; content: string }>
    }
    requests.push(body.messages)
    const round = requests.length
    if (round === 1) await firstResponse
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        message: `Reply ${round}`,
        proposed_edits: [],
        warnings: [],
      }),
    })
  })

  await page.goto('/')
  const input = page.getByLabel('Message Codex')
  await input.fill('First round')
  await input.press('Enter')
  await expect.poll(() => requests.length).toBe(1)
  await expect(input).toHaveAttribute('placeholder', 'Queue a message for the next round…')

  await input.fill('Second round')
  await input.press('Enter')
  await input.fill('Third round')
  await input.press('Enter')

  const queued = page.locator('.message.user.queued')
  await expect(queued).toHaveCount(2)
  await expect(queued.nth(0)).toContainText('Second round')
  await expect(queued.nth(0)).toContainText('Queued · round 1')
  await expect(queued.nth(1)).toContainText('Third round')
  await expect(queued.nth(1)).toContainText('Queued · round 2')
  expect(requests).toHaveLength(1)

  await input.press('ArrowUp')
  await expect(input).toHaveValue('Third round')
  await input.fill('')
  releaseFirst()

  await expect.poll(() => requests.length).toBe(3)
  await expect(page.locator('.message.assistant')).toHaveCount(3)
  await expect(queued).toHaveCount(0)
  await expect(page.getByLabel('Codex activity')).toHaveCount(0)
  await expect(input).toHaveAttribute('placeholder', 'Ask Codex to edit or inspect…')

  expect(requests[0].map(({ content }) => content)).toEqual(['First round'])
  expect(requests[1].map(({ content }) => content)).toEqual([
    'First round',
    'Reply 1',
    'Second round',
  ])
  expect(requests[2].map(({ content }) => content)).toEqual([
    'First round',
    'Reply 1',
    'Second round',
    'Reply 2',
    'Third round',
  ])
})

test('queued Codex rounds wait until preceding edits are reviewed', async ({ page }) => {
  const requests: string[] = []
  let applyRequests = 0
  let releaseFirst!: () => void
  const heldFirst = new Promise<void>((resolve) => {
    releaseFirst = resolve
  })
  await page.route('**/api/assistant/chat', async (route) => {
    const body = route.request().postDataJSON() as {
      messages: Array<{ role: string; content: string }>
    }
    requests.push(body.messages.at(-1)?.content ?? '')
    if (requests.length === 1) await heldFirst
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(requests.length === 1 ? {
        message: 'Review this edit before continuing.',
        proposed_edits: [{
          path: 'main.tex',
          content: 'reviewed content',
          summary: 'Update the manuscript',
          replacements: [{ old_text: 'old', new_text: 'new' }],
          version: null,
          is_new: false,
        }],
      } : {
        message: 'Queued round finished.',
        proposed_edits: [],
      }),
    })
  })
  await page.route('**/api/files/apply', (route) => {
    applyRequests += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        files: [{ path: 'main.tex', content: 'reviewed content', version: 'reviewed-version' }],
      }),
    })
  })

  await page.goto('/')
  const input = page.getByLabel('Message Codex')
  await input.fill('First round with an edit')
  await input.press('Enter')
  await expect.poll(() => requests.length).toBe(1)
  await input.fill('Queued second round')
  await input.press('Enter')
  releaseFirst()

  await expect(page.getByText('Review edits before the next Codex round')).toBeVisible()
  await expect(page.locator('.message.user.queued')).toContainText('Queued second round')
  await expect(input).toHaveAttribute('placeholder', 'Queue a message until edits are reviewed…')
  await page.waitForTimeout(250)
  expect(requests).toEqual(['First round with an edit'])

  page.once('dialog', (dialog) => void dialog.accept())
  await page.getByRole('button', { name: 'Apply', exact: true }).click()

  await expect.poll(() => applyRequests).toBe(1)
  await expect.poll(() => requests).toEqual([
    'First round with an edit',
    'Queued second round',
  ])
  await expect(page.getByText('Queued round finished.')).toBeVisible()
  await expect(page.getByText('Review edits before the next Codex round')).toHaveCount(0)
  await expect(page.locator('.message.user.queued')).toHaveCount(0)
})

test('project Codex history and edit-review state survive a page reload', async ({ page, request }) => {
  await page.route('**/api/assistant/chat', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      message: 'This response should survive a server restart.',
      proposed_edits: [{
        path: 'main.tex',
        content: 'persistent review content',
        summary: 'A persistent review card',
        replacements: [{ old_text: 'old', new_text: 'persistent' }],
        version: null,
        is_new: false,
      }],
      warnings: [],
    }),
  }))

  await page.goto('/')
  const input = page.getByLabel('Message Codex')
  await input.fill('Keep this request in project history')
  await input.press('Enter')
  await expect(page.getByText('This response should survive a server restart.')).toBeVisible()
  await expect(page.getByText('Review edits before the next Codex round')).toBeVisible()
  await expect.poll(async () => {
    const response = await request.get('/api/project/codex-history')
    const history = (await response.json()) as { messages: unknown[] }
    return history.messages.length
  }).toBe(2)

  await page.reload()
  await expect(page.getByText('Keep this request in project history')).toBeVisible()
  await expect(page.getByText('This response should survive a server restart.')).toBeVisible()
  await expect(page.getByText('A persistent review card')).toBeVisible()
  await expect(page.getByText('Review edits before the next Codex round')).toBeVisible()
  await input.press('ArrowUp')
  await expect(input).toHaveValue('Keep this request in project history')

  await page.getByRole('button', { name: 'Dismiss', exact: true }).click()
  await expect(page.getByText('Review edits before the next Codex round')).toHaveCount(0)
  await expect.poll(async () => {
    const response = await request.get('/api/project/codex-history')
    const history = (await response.json()) as { dismissed_edit_keys: string[] }
    return history.dismissed_edit_keys
  }).toEqual(['1-0'])

  await page.reload()
  await expect(page.getByText('Keep this request in project history')).toBeVisible()
  await expect(page.getByText('This response should survive a server restart.')).toBeVisible()
  await expect(page.getByText('A persistent review card')).toHaveCount(0)
  await expect(page.getByText('Review edits before the next Codex round')).toHaveCount(0)
})

test('a running Codex turn can be cancelled and its queued rounds are cleared', async ({ page }) => {
  let requestId = ''
  let cancelledRequestId = ''
  let releaseChat!: () => void
  const heldChat = new Promise<void>((resolve) => {
    releaseChat = resolve
  })
  await page.route('**/api/assistant/chat', async (route) => {
    requestId = (route.request().postDataJSON() as { request_id: string }).request_id
    await heldChat
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ message: 'A response that should be discarded.', proposed_edits: [] }),
    }).catch(() => undefined)
  })
  await page.route('**/api/assistant/cancel/*', (route) => {
    cancelledRequestId = route.request().url().split('/').pop() ?? ''
    releaseChat()
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, message: 'Codex turn cancellation requested' }),
    })
  })

  await page.goto('/')
  const input = page.getByLabel('Message Codex')
  await input.fill('Long-running round')
  await input.press('Enter')
  await expect.poll(() => requestId).not.toBe('')
  await input.fill('Queued follow-up')
  await input.press('Enter')
  await expect(page.locator('.message.user.queued')).toHaveCount(1)

  await page.getByRole('button', { name: 'Cancel Codex turn' }).click()

  await expect(page.locator('.message.assistant').last()).toContainText('Codex turn cancelled')
  await expect(page.locator('.message.assistant').last()).toContainText('1 queued round cleared')
  await expect(page.locator('.message.user.queued')).toHaveCount(0)
  await expect(page.getByLabel('Codex activity')).toHaveCount(0)
  await expect(input).toHaveAttribute('placeholder', 'Ask Codex to edit or inspect…')
  expect(cancelledRequestId).toBe(requestId)
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

test('a confirmed Codex edit creates a project-local skill', async ({ page, request }) => {
  const skillDirectory = '.codex/skills/manuscript-helper'
  const skillPath = `${skillDirectory}/SKILL.md`
  const skillContent = [
    '---',
    'name: manuscript-helper',
    'description: Improve this project manuscript.',
    '---',
    '',
    'Inspect the manuscript and make focused edits.',
    '',
  ].join('\n')
  await request.delete(`/api/files/${skillDirectory}`).catch(() => undefined)
  await page.route('**/api/assistant/chat', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        message: 'Created a project-local Codex skill for review.',
        proposed_edits: [{
          path: skillPath,
          content: skillContent,
          summary: 'Create the manuscript helper skill',
          version: null,
          is_new: true,
        }],
      }),
    }),
  )

  try {
    await page.goto('/')
    await page.getByLabel('Message Codex').fill('Create a project-local manuscript skill')
    await page.getByLabel('Send message').click()
    await expect(page.locator('.proposed-edit')).toContainText(skillPath)
    expect((await request.get(`/api/files/${skillPath}`)).status()).toBe(404)

    page.once('dialog', (dialog) => dialog.accept())
    await page.getByRole('button', { name: 'Apply' }).click()
    await expect(page.locator('.project-path')).toContainText(skillPath)
    await expect(page.locator('.cm-content')).toContainText('name: manuscript-helper')
    expect(
      ((await (await request.get(`/api/files/${skillPath}`)).json()) as { content: string }).content,
    ).toBe(skillContent)

    await page.getByLabel('Expand .codex').click()
    await page.getByLabel('Expand skills').click()
    await page.getByLabel('Expand manuscript-helper').click()
    await expect(page.getByRole('button', { name: 'SKILL.md', exact: true })).toBeVisible()
    await expect(page.getByLabel('Rename .codex', { exact: true })).toHaveCount(1)
    await expect(page.getByLabel('Delete .codex', { exact: true })).toHaveCount(1)
  } finally {
    await request.delete('/api/files/.codex/skills').catch(() => undefined)
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

test('terminal tab confirms launcher restart and shutdown', async ({ page }) => {
  let shutdownRequests = 0
  let restartRequests = 0
  await page.route('**/api/runtime', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        managed: true,
        log_available: false,
        shutdown_available: true,
        restart_available: true,
      }),
    }),
  )
  await page.route('**/api/health', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) }),
  )
  await page.route('**/api/runtime/restart', (route) => {
    restartRequests += 1
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, message: 'Server restart requested' }),
    })
  })
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
  const restart = page.getByRole('button', { name: 'Restart server' })
  await expect(shutdown).toBeEnabled()
  await expect(restart).toBeEnabled()

  page.once('dialog', (dialog) => void dialog.dismiss())
  await restart.click()
  expect(restartRequests).toBe(0)

  page.once('dialog', (dialog) => void dialog.accept())
  await restart.click()
  await expect.poll(() => restartRequests).toBe(1)
  await expect(restart).toBeEnabled({ timeout: 5_000 })

  page.once('dialog', (dialog) => void dialog.dismiss())
  await shutdown.click()
  expect(shutdownRequests).toBe(0)

  page.once('dialog', (dialog) => void dialog.accept())
  await shutdown.click()
  await expect(page.getByText('Server shutdown requested. This page will disconnect.')).toBeVisible()
  expect(shutdownRequests).toBe(1)
})

test('status tab reports component and service operating conditions', async ({ page }) => {
  await page.route('**/api/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        overall: 'degraded',
        checked_at: new Date().toISOString(),
        services: [
          { id: 'backend', name: 'Backend API', condition: 'healthy', detail: 'FastAPI is responding' },
          { id: 'project', name: 'Project workspace', condition: 'healthy', detail: '/tmp/project · compiling main.tex' },
          { id: 'git', name: 'Git checkpoints', condition: 'healthy', detail: 'Local checkpoints active' },
          { id: 'compiler', name: 'LaTeX compiler', condition: 'degraded', detail: 'The latest compilation failed' },
          { id: 'assistant', name: 'Codex provider', condition: 'healthy', detail: 'Codex SDK and CLI available' },
          { id: 'launcher', name: 'Managed launcher', condition: 'healthy', detail: 'Managed launcher active' },
        ],
      }),
    }),
  )

  await page.goto('/')
  await page.getByRole('tab', { name: 'Status' }).click()
  await expect(page.getByRole('tabpanel', { name: 'System status' })).toBeVisible()
  await expect(page.getByText('1 service need attention')).toBeVisible()
  await expect(page.locator('.service-status')).toHaveCount(7)
  await expect(page.locator('.service-status.degraded')).toContainText('LaTeX compiler')
  await expect(page.getByText('Git checkpoints')).toBeVisible()
  await expect(page.getByText('Codex provider')).toBeVisible()
})
