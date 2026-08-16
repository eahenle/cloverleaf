import { expect, test, type Page } from '@playwright/test'
import path from 'node:path'

async function drag(page: Page, label: string, axis: 'x' | 'y', amount: number) {
  const handle = page.getByLabel(label)
  const before = await handle.boundingBox()
  expect(before).not.toBeNull()
  const startX = before!.x + before!.width / 2
  const startY = before!.y + before!.height / 2
  await page.mouse.move(startX, startY)
  await page.mouse.down()
  await page.mouse.move(startX + (axis === 'x' ? amount : 0), startY + (axis === 'y' ? amount : 0))
  await page.mouse.up()
  const after = await handle.boundingBox()
  expect(after).not.toBeNull()
  expect(Math.abs((axis === 'x' ? after!.x - before!.x : after!.y - before!.y))).toBeGreaterThan(5)
}

for (const viewport of [
  { width: 1280, height: 720 },
  { width: 1600, height: 1000 },
]) {
  test(`desktop layout is contained and usable at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await page.goto('/')
    await expect(page.locator('.topbar-build')).toHaveClass(/success/, { timeout: 30_000 })
    for (const label of ['Project files', 'Source editor', 'PDF preview', 'Codex assistant']) {
      await expect(page.getByLabel(label)).toBeVisible()
    }
    const dimensions = await page.locator('body').evaluate((body) => ({
      clientWidth: body.clientWidth,
      scrollWidth: body.scrollWidth,
      clientHeight: body.clientHeight,
      scrollHeight: body.scrollHeight,
    }))
    expect(dimensions.scrollWidth).toBe(dimensions.clientWidth)
    expect(dimensions.scrollHeight).toBe(dimensions.clientHeight)
    const chatInput = await page.getByLabel('Message Codex').boundingBox()
    expect(chatInput).not.toBeNull()
    expect(chatInput!.y + chatInput!.height).toBeLessThanOrEqual(viewport.height)

    if (viewport.width === 1600) {
      await drag(page, 'Resize project and compiler panels', 'y', 24)
      await drag(page, 'Resize project and editor panels', 'x', 24)
      await drag(page, 'Resize editor and preview panels', 'x', -24)
      await drag(page, 'Resize preview and assistant panels', 'y', -24)
      await page.getByRole('tab', { name: 'Terminal' }).click()
      await expect(page.getByLabel('Server terminal')).toBeVisible()
      await expect(page.getByRole('button', { name: 'Shut down server' })).toBeDisabled()
    }

    await page.screenshot({
      path: path.resolve(process.cwd(), `../screenshots/desktop-${viewport.width}x${viewport.height}.png`),
      fullPage: true,
    })
  })
}
