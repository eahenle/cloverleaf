import { expect, test } from '@playwright/test'
import { clearAssistantHistory } from './history'

function linkedPdf() {
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 600] /Annots [5 0 R 6 0 R 7 0 R] >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 600] >>',
    '<< /Type /Annot /Subtype /Link /Rect [20 500 140 520] /A << /S /URI /URI (https://example.com/article) >> >>',
    '<< /Type /Annot /Subtype /Link /Rect [20 450 140 470] /Dest [4 0 R /Fit] >>',
    '<< /Type /Annot /Subtype /Link /Rect [20 400 140 420] /A << /S /URI /URI (javascript:alert%281%29) >> >>',
  ]
  let pdf = '%PDF-1.4\n'
  const offsets = [0]
  objects.forEach((object, index) => { offsets.push(Buffer.byteLength(pdf)); pdf += `${index + 1} 0 obj\n${object}\nendobj\n` })
  const start = Buffer.byteLength(pdf)
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
  pdf += offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`
  return Buffer.from(pdf)
}

test('PDF links overlay the canvas, resize with it, and navigate safely', async ({ page, request }) => {
  await clearAssistantHistory(request)
  const status = { state: 'success', diagnostics: [], log_tail: '', revision: 999 }
  await page.route('**/api/compile', route => route.fulfill({ json: status }))
  await page.route('**/api/compile/status', route => route.fulfill({ json: status }))
  await page.route('**/api/pdf?revision=999', route => route.fulfill({ contentType: 'application/pdf', body: linkedPdf() }))
  await page.goto('/')
  const external = page.getByRole('link', { name: 'https://example.com/article', exact: true })
  await expect(external).toBeVisible()
  await expect(external).toHaveAttribute('href', 'https://example.com/article')
  await expect(external).toHaveAttribute('target', '_blank')
  await expect(external).toHaveAttribute('rel', 'noopener noreferrer')
  await expect(page.locator('.pdf-page-container a[href^="javascript:"]')).toHaveCount(0)
  const checkPosition = async () => {
    const canvas = await page.getByTestId('pdf-page').first().boundingBox()
    const link = await external.boundingBox()
    expect(canvas).not.toBeNull()
    expect(link).not.toBeNull()
    expect(link!.x - canvas!.x).toBeCloseTo(canvas!.width * 20 / 300, 0)
    expect(link!.y - canvas!.y).toBeCloseTo(canvas!.height * 80 / 600, 0)
  }
  await checkPosition()
  await page.setViewportSize({ width: 1600, height: 1000 })
  await checkPosition()
  await page.context().route('https://example.com/article', route => route.fulfill({ body: 'Linked article' }))
  const popupPromise = page.waitForEvent('popup')
  await external.click()
  const popup = await popupPromise
  await expect(popup).toHaveURL('https://example.com/article')
  await popup.close()
  await page.getByRole('link', { name: 'Go to document destination' }).click()
  await expect.poll(() => page.locator('.pdf-scroll').evaluate(element => element.scrollTop)).toBeGreaterThan(200)
})
