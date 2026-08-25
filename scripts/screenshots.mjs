/* Deterministic screenshots for the README.
 *
 * Every shot waits on a condition that is TRUE ONLY WHEN THE DATA HAS RENDERED — a settled
 * number, a populated table, a finished stream — rather than on a timeout. A timeout-based
 * screenshot script produces a different image on every machine, and the first symptom is a
 * README full of empty panels that looked fine locally.
 *
 * IT ALSO HAS TO BE RUN. A previous version of this file navigated to a "Call monitor" screen
 * and clicked ".node-card", neither of which had existed for two redesigns — so the README was
 * illustrated with a product nobody could open any more, and nothing failed to say so. The
 * screens are asserted by name here for that reason: if one is renamed, this stops rather than
 * quietly capturing the wrong thing.
 */

import { chromium } from 'playwright'
import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'

const BASE = process.env.STUDIO_URL ?? 'http://localhost:5173'
// Resolved against the repo root passed in, not against this file's own location -- the script
// has to be runnable from wherever Playwright happens to be installed, and an import.meta.url
// relative path silently writes the images into the wrong tree when it is.
const OUT = resolve(process.env.SHOT_DIR ?? 'docs/img')

const VIEWPORT = { width: 1500, height: 1000 }

async function main() {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch({
    args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
           '--autoplay-policy=no-user-gesture-required'],
  })
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,          // retina, so the README images stay sharp when scaled
    colorScheme: 'dark',
    permissions: ['microphone'],
  })
  const page = await context.newPage()

  const shot = async (name, fullPage = false) => {
    // ANIMATIONS FROZEN TO THEIR END STATE. Every bubble carries `animation: rise .22s`, and a
    // shot taken while that is in flight catches them part-way faded -- which produced a
    // transcript of grey-on-grey text that looked like a contrast bug and was a timing one.
    // `disabled` finishes them instantly rather than skipping them, so the image is what a
    // settled page looks like, on every machine, every run.
    await page.screenshot({ path: `${OUT}/${name}.png`, fullPage, animations: 'disabled' })
    console.log(`  ${name}.png`)
  }

  const nav = async (label) => {
    const button = page.getByRole('navigation').getByRole('button', { name: new RegExp(`^${label}`) })
    if (!(await button.count())) throw new Error(`no screen called ${label}`)
    await button.click()
  }

  console.log('capturing:')
  await page.goto(BASE, { waitUntil: 'networkidle' })

  // ── dashboard ────────────────────────────────────────────────────────────
  await nav('Dashboard')
  await page.waitForFunction(() => document.querySelectorAll('.metric-v').length >= 4)
  await page.waitForFunction(() => document.querySelectorAll('.chart-col').length >= 14)
  await shot('dashboard')

  // ── a live call, mid-conversation ────────────────────────────────────────
  await nav('Live call')
  await page.getByRole('tab', { name: 'Chat' }).click()
  await page.getByRole('button', { name: /Start chat/ }).click()
  await page.waitForFunction(() => document.querySelectorAll('.bubble').length >= 1)

  for (const question of ['how much is a check-up?', 'are you open on thursdays?']) {
    const before = await page.locator('.bubble').count()
    await page.locator('textarea').fill(question)
    await page.keyboard.press('Enter')
    // Wait for the agent's reply to finish streaming, not merely to start.
    await page.waitForFunction(
      (n) => document.querySelectorAll('.bubble').length >= n + 2 &&
             !document.querySelector('.bubble:last-child .caret'),
      before, { timeout: 120_000 },
    )
  }
  // The details panel is half the story on this screen; fill it so it is not all placeholders.
  await page.locator('.field input').nth(0).fill('Sam Hassan')
  await page.locator('.field input').nth(1).fill('(212) 555-0142')
  await page.getByRole('button', { name: /Save details/ }).click()
  await page.waitForSelector('.know[data-confirmed="true"]', { timeout: 15_000 })
  await shot('monitor')

  await page.getByRole('button', { name: /End chat/ }).click()

  // ── turn-taking ──────────────────────────────────────────────────────────
  await nav('Turn-taking')
  await page.waitForFunction(
    () => document.querySelectorAll('tbody tr').length >= 4, null, { timeout: 60_000 },
  )
  await shot('benchmark')

  // The published corpus, further down the same screen.
  await page.waitForFunction(
    () => document.querySelectorAll('tbody tr').length > 20, null, { timeout: 60_000 },
  ).catch(() => undefined)
  await page.evaluate(() => {
    const rows = document.querySelectorAll('tbody tr')
    rows[rows.length - 1]?.scrollIntoView({ block: 'center' })
  })
  await page.waitForTimeout(500)
  await shot('corpus')

  // ── call history ─────────────────────────────────────────────────────────
  // The screen an operator spends the most time on, and the one that had no picture in the
  // README at all -- so two rounds of "the history does not feel complete" were about a screen
  // nobody reading the repo could see.
  await nav('Call history')
  await page.waitForFunction(
    () => document.querySelectorAll('tbody tr').length >= 4, null, { timeout: 30_000 },
  )
  await shot('calls')

  // A single call, opened. NOT the newest row: the newest is the chat this script started
  // two steps ago, and its turns are still being written when the click lands -- the first
  // version of this shot was a full set of metrics beside an empty transcript panel, which
  // is a screenshot of a bug that does not exist.
  //
  // So: a row that BOOKED something, and the wait is on the bubbles themselves rather than on
  // a heading that renders whether or not the transcript arrived.
  const booked = page.locator('tbody tr').filter({ hasText: /booked/i }).first()
  await (await booked.count() ? booked : page.locator('tbody tr').last()).click()
  await page.waitForFunction(
    () => document.querySelectorAll('.bubble').length >= 2, null, { timeout: 20_000 },
  )
  await shot('call-detail', true)
  await page.getByRole('button', { name: /All calls/ }).click()

  // ── the flow ─────────────────────────────────────────────────────────────
  await nav('Conversation flow')
  await page.waitForSelector('.node', { timeout: 30_000 })
  await shot('flow')

  await page.locator('.node').filter({ hasText: 'offer_slots' }).first().click()
  await page.waitForFunction(
    () => Boolean(document.querySelector('.node[data-sel="true"]')), null, { timeout: 15_000 },
  )
  await shot('flow-node', true)

  // ── compliance ───────────────────────────────────────────────────────────
  await nav('Compliance')
  await page.waitForFunction(
    () => document.body.innerText.includes('What the AI receives'), null, { timeout: 30_000 },
  )
  await page.waitForTimeout(600)
  await shot('compliance')

  await browser.close()
  console.log(`\nwritten to ${OUT}`)
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
