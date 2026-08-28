import type { APIRequestContext } from '@playwright/test'

export async function clearAssistantHistory(request: APIRequestContext): Promise<void> {
  const projectResponse = await request.get('/api/project')
  if (!projectResponse.ok()) {
    throw new Error(`Could not read the active project: ${projectResponse.status()}`)
  }
  const project = (await projectResponse.json()) as { workspace: string }
  const response = await request.put('/api/project/codex-history', {
    data: {
      version: 1,
      workspace: project.workspace,
      messages: [],
      dismissed_edit_keys: [],
    },
  })
  if (!response.ok()) {
    throw new Error(`Could not clear Codex history: ${response.status()}`)
  }
}
