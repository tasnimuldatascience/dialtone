/* Deterministic screenshots for the README.
 *
 * Every shot waits on a condition that is TRUE ONLY WHEN THE DATA HAS RENDERED — a settled
 * number, a populated table, a finished stream — rather than on a timeout. A timeout-based
 * screenshot script produces a different image on every machine, and the first symptom is a
 * README full of empty panels that looked fine locally.
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
  const browser = await chromium.launch()
  const page = await browser.newPage({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,          // retina, so the README images stay sharp when scaled
    colorScheme: 'dark',
  })

  const shot = async (name, fullPage = false) => {
    await page.screenshot({ path: `${OUT}/${name}.png`, fullPage })
    console.log(`  ${name}.png`)
  }

  const nav = async (label) => {
    await page.getByRole('button', { name: label, exact: true }).click()
  }

  console.log('capturing:')

  // ── benchmark ────────────────────────────────────────────────────────────
  await page.goto(BASE, { waitUntil: 'networkidle' })
  // The chart only has content once the sweep resolves; circles are the last thing drawn.
  await page.waitForSelector('svg circle', { timeout: 30_000 })
  await page.waitForFunction(
    () => document.querySelectorAll('tbody tr').length >= 5,
    { timeout: 30_000 },
  )
  await shot('benchmark')

  // ── call monitor: run a barge-in call to completion ──────────────────────
  await nav('Call monitor')
  await page.getByRole('button', { name: 'Caller interrupts mid-sentence' }).click()
  await page.getByRole('button', { name: 'Replay call' }).click()
  // Wait for the stream to finish AND the transcript panel to arrive. Screenshotting on the
  // last event alone catches the page one render before the summary exists.
  await page.waitForSelector('.tl-row[data-kind="barge_in"]', { timeout: 60_000 })
  await page.waitForFunction(
    () => document.body.innerText.includes('What the model sees next turn'),
    { timeout: 60_000 },
  )
  await page.waitForTimeout(400)
  await shot('monitor')

  // ── a call where a number is read aloud ──────────────────────────────────
  await page.getByRole('button', { name: 'Caller reads a card number' }).click()
  await page.getByRole('button', { name: 'Replay call' }).click()
  await page.waitForFunction(
    () => document.body.innerText.includes('Removed before anything stored it'),
    { timeout: 60_000 },
  )
  await page.waitForTimeout(400)
  await shot('monitor-redaction', true)

  // ── flow ─────────────────────────────────────────────────────────────────
  await nav('Flow')
  await page.waitForSelector('.node-card', { timeout: 30_000 })
  await shot('flow')
  await page.getByRole('button', { name: /offer_slots/ }).click()
  await page.waitForFunction(
    () => document.body.innerText.includes('what it may do'),
    { timeout: 15_000 },
  )
  await shot('flow-node', true)

  // ── corpus ───────────────────────────────────────────────────────────────
  await nav('Corpus')
  await page.waitForFunction(
    () => document.querySelectorAll('tbody tr').length > 20,
    { timeout: 30_000 },
  )
  await shot('corpus')

  // ── compliance ───────────────────────────────────────────────────────────
  await nav('Compliance')
  await page.waitForSelector('mark.strip', { timeout: 30_000 })
  await shot('compliance')

  await browser.close()
  console.log(`\nwritten to ${OUT}`)
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
