import { expect, test } from '@playwright/test'

test('attachments stay in their project and clear when switching to a same-named file', async ({ page }) => {
  let workspace = '/projects/first'
  const requests: Array<{ attached_paths: string[] }> = []
  const status = { state: 'idle', diagnostics: [], log_tail: '', revision: 0 }
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    let body: unknown = {}
    if (url.pathname === '/api/project/load') {
      workspace = (route.request().postDataJSON() as { workspace: string }).workspace
      body = { workspace, name: workspace.split('/').pop(), main_file: 'main.tex' }
    } else if (url.pathname === '/api/project') {
      body = { workspace, name: 'first', main_file: 'main.tex' }
    } else if (url.pathname === '/api/project/tree') {
      body = [{ type: 'file', name: 'main.tex', path: 'main.tex' }]
    } else if (url.pathname === '/api/project/directories') {
      const directory = url.searchParams.get('path') ?? workspace
      body = { path: directory, parent: '/projects', root: '/', home: '/projects',
        directories: directory === '/projects/first' ? [{ name: 'second', path: '/projects/second' }] : [],
        tex_files: ['main.tex'] }
    } else if (url.pathname === '/api/files/main.tex') {
      body = { path: 'main.tex', content: `Project ${workspace}`, version: workspace }
    } else if (url.pathname.startsWith('/api/compile')) {
      body = status
    } else if (url.pathname === '/api/assistant/chat') {
      requests.push(route.request().postDataJSON() as { attached_paths: string[] })
      body = { message: 'Done', proposed_edits: [] }
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.goto('/')
  await page.getByLabel('Attach a project file').selectOption('main.tex')
  // Opening and cancelling navigation must not discard a valid selection.
  await page.getByLabel('Load project').click()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(page.getByLabel('Attached files')).toContainText('main.tex')
  await page.getByLabel('Message Codex').fill('Inspect first project')
  await page.getByLabel('Send message').click()
  await expect.poll(() => requests.length).toBe(1)
  expect(requests[0].attached_paths).toEqual(['main.tex'])
  await expect(page.getByLabel('Attached files')).toHaveCount(0)
  await expect(page.getByLabel('Send message')).toBeDisabled()
  await page.getByLabel('Attach a project file').selectOption('main.tex')
  await page.getByLabel('Load project').click()
  const picker = page.getByRole('dialog', { name: 'Load project' })
  await picker.getByRole('button', { name: 'Open folder second', exact: true }).click()
  await picker.getByRole('button', { name: 'Open project', exact: true }).click()
  await expect(picker).toHaveCount(0)
  await expect(page.locator('.project-path')).toContainText('second / main.tex')
  await page.getByLabel('Message Codex').fill('Inspect second project')
  await page.getByLabel('Send message').click()
  await expect.poll(() => requests.length).toBe(2)
  expect(requests[1].attached_paths).toEqual([])
})
