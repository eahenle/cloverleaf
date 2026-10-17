import { expect, test, type Page, type Route } from '@playwright/test'

type ChatRequest = {
  messages: Array<{ role: string; content: string }>
  context: Record<string, unknown>
  attached_paths: string[]
  request_id: string
}

async function mockProject(page: Page, onChat: (route: Route, body: ChatRequest) => Promise<void>) {
  let workspace = '/projects/first'
  let mainFile = 'main.tex'
  const status = { state: 'idle', diagnostics: [], log_tail: '', revision: 0 }
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    let body: unknown = {}
    if (url.pathname === '/api/assistant/chat') {
      await onChat(route, route.request().postDataJSON() as ChatRequest)
      return
    }
    if (url.pathname === '/api/project/load') {
      workspace = (route.request().postDataJSON() as { workspace: string }).workspace
      mainFile = 'main.tex'
      body = { workspace, name: workspace.split('/').pop(), main_file: mainFile }
    } else if (url.pathname === '/api/project/compilation-root') {
      mainFile = (route.request().postDataJSON() as { main_file: string }).main_file
      body = { workspace, name: workspace.split('/').pop(), main_file: mainFile }
    } else if (url.pathname === '/api/project') {
      body = { workspace, name: workspace.split('/').pop(), main_file: mainFile }
    } else if (url.pathname === '/api/project/codex-history') {
      body = route.request().method() === 'PUT'
        ? route.request().postDataJSON()
        : { version: 1, workspace, messages: [], dismissed_edit_keys: [] }
    } else if (url.pathname === '/api/project/tree') {
      body = [
        { type: 'file', name: 'main.tex', path: 'main.tex', children: [] },
        { type: 'directory', name: 'sections', path: 'sections', children: [
          { type: 'file', name: 'intro.tex', path: 'sections/intro.tex', children: [] },
        ] },
      ]
    } else if (url.pathname === '/api/project/directories') {
      const directory = url.searchParams.get('path') ?? workspace
      body = {
        path: directory, parent: '/projects', root: '/', home: '/projects',
        directories: directory === '/projects/first' ? [{ name: 'second', path: '/projects/second' }] : [],
        tex_files: ['main.tex'],
      }
    } else if (url.pathname.startsWith('/api/files/')) {
      body = { path: url.pathname.slice('/api/files/'.length), content: `Project ${workspace}`, version: workspace }
    } else if (url.pathname.startsWith('/api/compile')) {
      body = status
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
}

async function reply(route: Route, message = 'Done', proposedEdits: unknown[] = []) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ message, proposed_edits: proposedEdits }),
  })
}

test('attachments clear on workspace switches but survive cancelled project navigation', async ({ page }) => {
  const requests: ChatRequest[] = []
  await mockProject(page, async (route, body) => {
    requests.push(body)
    await reply(route)
  })
  await page.goto('/')
  const pickerControl = page.getByLabel('Attach a project file')
  await pickerControl.selectOption('main.tex')
  await expect(pickerControl.locator('option[value="main.tex"]')).toHaveCount(0)
  await page.getByLabel('Load project').click()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(page.getByLabel('Attached files')).toContainText('main.tex')
  await page.getByLabel('Message Codex').fill('Inspect first project')
  await page.getByLabel('Send message').click()
  await expect(page.locator('.message.assistant')).toHaveCount(1)
  expect(requests[0].attached_paths).toEqual(['main.tex'])
  expect(requests[0].context).not.toHaveProperty('attached_files')
  await expect(page.getByLabel('Attached files')).toHaveCount(0)

  await pickerControl.selectOption('main.tex')
  await page.getByLabel('Message Codex').fill('Unsent first-project draft')
  await page.getByLabel('Load project').click()
  const picker = page.getByRole('dialog', { name: 'Load project' })
  await picker.getByRole('button', { name: 'Open folder second', exact: true }).click()
  await picker.getByRole('button', { name: 'Open project', exact: true }).click()
  await expect(picker).toHaveCount(0)
  await expect(page.locator('.project-path')).toContainText('second / main.tex')
  await expect(page.getByLabel('Attached files')).toHaveCount(0)
  await expect(page.getByLabel('Message Codex')).toHaveValue('')
  await page.getByLabel('Message Codex').fill('Inspect second project')
  await page.getByLabel('Send message').click()
  await expect.poll(() => requests.length).toBe(2)
  expect(requests[1].attached_paths).toEqual([])
})

test('changing the compilation root preserves the current attachment selection', async ({ page }) => {
  await mockProject(page, async (route) => reply(route))
  await page.goto('/')
  await page.getByLabel('Attach a project file').selectOption('main.tex')
  await page.getByLabel('Expand sections').click()
  await page.getByRole('button', { name: 'intro.tex', exact: true }).click()
  const compileView = page.getByRole('button', { name: 'Compile sections/intro.tex as the current view', exact: true })
  await compileView.click()
  await expect(compileView).toHaveCount(0)
  await expect(page.getByLabel('Attached files')).toContainText('main.tex')
})

test('queued turns retain their own attachments while the next draft changes', async ({ page }) => {
  const requests: ChatRequest[] = []
  let releaseFirst!: () => void
  const firstResponse = new Promise<void>((resolve) => { releaseFirst = resolve })
  await mockProject(page, async (route, body) => {
    requests.push(body)
    const round = requests.length
    if (round === 1) await firstResponse
    await reply(route, `Reply ${round}`)
  })
  await page.goto('/')
  const input = page.getByLabel('Message Codex')
  const picker = page.getByLabel('Attach a project file')
  await picker.selectOption('main.tex')
  await input.fill('First round')
  await input.press('Enter')
  await expect.poll(() => requests.length).toBe(1)
  await expect(picker).toBeEnabled()
  await picker.selectOption('sections/intro.tex')
  await input.fill('Second round')
  await input.press('Enter')
  await expect(page.getByLabel('Attached files')).toHaveCount(0)
  await input.fill('Third round')
  await input.press('Enter')
  await expect(page.locator('.message.user.queued')).toHaveCount(2)

  await picker.selectOption('main.tex')
  await page.getByLabel('Remove main.tex', { exact: true }).click()
  await expect(page.getByLabel('Attached files')).toHaveCount(0)
  await input.press('ArrowUp')
  await expect(input).toHaveValue('Third round')
  await input.fill('Unsent next draft')
  await picker.selectOption('main.tex')
  releaseFirst()

  await expect(page.locator('.message.assistant')).toHaveCount(3)
  await expect(page.locator('.message.user.queued')).toHaveCount(0)
  expect(requests.map((body) => body.messages.at(-1)?.content)).toEqual([
    'First round', 'Second round', 'Third round',
  ])
  expect(requests.map((body) => body.attached_paths)).toEqual([
    ['main.tex'], ['sections/intro.tex'], [],
  ])
  await expect(input).toHaveValue('Unsent next draft')
  await expect(page.getByLabel('Attached files')).toContainText('main.tex')
})

test('attachment snapshots survive the edit-review pause before queued dispatch', async ({ page }) => {
  const requests: ChatRequest[] = []
  await mockProject(page, async (route, body) => {
    requests.push(body)
    await reply(route, 'Done', requests.length === 1 ? [{
      path: 'main.tex', content: 'Reviewed content', summary: 'A reviewable edit',
      version: null, is_new: false,
    }] : [])
  })
  await page.goto('/')
  const input = page.getByLabel('Message Codex')
  await input.fill('Prepare an edit')
  await input.press('Enter')
  await expect(page.getByText('Review edits before the next Codex round')).toBeVisible()
  await page.getByLabel('Attach a project file').selectOption('sections/intro.tex')
  await input.fill('Inspect the attached introduction')
  await input.press('Enter')
  await expect(page.locator('.message.user.queued')).toHaveCount(1)
  expect(requests).toHaveLength(1)
  await page.getByRole('button', { name: 'Dismiss', exact: true }).click()
  await expect(page.locator('.message.assistant')).toHaveCount(2)
  expect(requests[1].attached_paths).toEqual(['sections/intro.tex'])
})

test('cancelled queued attachments do not leak into a later turn', async ({ page }) => {
  const requests: ChatRequest[] = []
  let releaseFirst!: () => void
  const firstResponse = new Promise<void>((resolve) => { releaseFirst = resolve })
  await mockProject(page, async (route, body) => {
    requests.push(body)
    if (requests.length === 1) await firstResponse
    await reply(route).catch(() => undefined)
  })
  await page.route('**/api/assistant/cancel/*', async (route) => {
    releaseFirst()
    await route.fulfill({ status: 202, contentType: 'application/json', body: '{"ok":true}' })
  })
  await page.goto('/')
  const input = page.getByLabel('Message Codex')
  await input.fill('Long-running round')
  await input.press('Enter')
  await expect.poll(() => requests.length).toBe(1)
  await page.getByLabel('Attach a project file').selectOption('main.tex')
  await input.fill('Queued with attachment')
  await input.press('Enter')
  await expect(page.locator('.message.user.queued')).toHaveCount(1)
  await page.getByRole('button', { name: 'Cancel Codex turn' }).click()
  await expect(page.getByLabel('Codex activity')).toHaveCount(0)
  await expect(page.locator('.message.user.queued')).toHaveCount(0)
  await input.fill('Fresh round')
  await input.press('Enter')
  await expect.poll(() => requests.length).toBe(2)
  expect(requests[1].messages.at(-1)?.content).toBe('Fresh round')
  expect(requests[1].attached_paths).toEqual([])
})
