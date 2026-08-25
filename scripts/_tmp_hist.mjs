import { chromium } from 'playwright'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
await page.goto('http://localhost:5173', { waitUntil: 'networkidle' })
await page.getByRole('navigation').getByRole('button', { name: 'Call history', exact: true }).click()
await page.waitForSelector('tbody tr')
// Open the longest call.
const rows = page.locator('tbody tr')
let best = 0, bestI = 0
for (let i = 0; i < await rows.count(); i++) {
  const t = await rows.nth(i).innerText()
  const turns = parseInt(t.split('\t').at(-3) ?? '0', 10) || 0
  if (turns > best) { best = turns; bestI = i }
}
await rows.nth(bestI).click()
await page.waitForTimeout(1800)
await page.screenshot({ path: process.env.OUT })
await browser.close()
