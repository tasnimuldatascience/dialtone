/**
 * Drives the studio in a real browser and reports anything broken.
 *
 * WHY THIS EXISTS. The server log shows what reached the server. It is silent about everything
 * that fails in the browser — a component that throws on render, a request that never goes out,
 * a permission that is refused, audio that never plays. Several bugs in this project were only
 * found because someone opened the page and described what they saw, which is not a test
 * strategy.
 *
 * It checks the things a person would check first:
 *   every screen renders without a console error
 *   a voice call opens with the microphone already live
 *   one question produces exactly one reply
 *
 * Run it with both servers up:
 *     node scripts/smoke.mjs
 *
 * Requires playwright, which is a devDependency of apps/studio, so run it from there or with
 * NODE_PATH pointing at that install.
 */

import { chromium } from 'playwright'

const BASE = process.env.STUDIO_URL ?? 'http://localhost:5173'

const SCREENS = [
  'Dashboard', 'Live call', 'Call history', 'Agents', 'Knowledge',
  'Conversation flow', 'Phone numbers', 'Turn-taking', 'Compliance',
]

const browser = await chromium.launch({
  args: [
    // A microphone that always exists and always grants permission, so the voice path can be
    // exercised without a person present.
    '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream',
    '--autoplay-policy=no-user-gesture-required',
  ],
})

const context = await browser.newContext({ permissions: ['microphone'] })
const page = await context.newPage()

const problems = []
page.on('console', (m) => {
  if (m.type() === 'error' || m.type() === 'warning') problems.push(`[${m.type()}] ${m.text()}`)
})
page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`))
page.on('requestfailed', (r) => problems.push(`[network] ${r.url()} ${r.failure()?.errorText}`))

let failures = 0
const check = (label, ok, detail = '') => {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? `  (${detail})` : ''}`)
  if (!ok) failures += 1
}

console.log('screens render:')
await page.goto(BASE, { waitUntil: 'networkidle' })
const nav = page.getByRole('navigation')
for (const screen of SCREENS) {
  const before = problems.length
  await nav.getByRole('button', { name: screen, exact: true }).click()
  await page.waitForTimeout(1300)
  check(screen, problems.length === before)
}

console.log('\nvoice call:')
await nav.getByRole('button', { name: 'Live call', exact: true }).click()
await page.getByText('Speak replies').click()
await page.waitForTimeout(300)
await page.getByRole('button', { name: /Start call/ }).click()
await page.waitForTimeout(6000)

check('microphone starts with the call', (await page.locator('.mic[data-on="true"]').count()) > 0)

const engine = await page.locator('.chip', { hasText: /voice$/ }).first().textContent().catch(() => '')
check('neural voice is in use', /neural/.test(engine ?? ''), engine ?? 'no chip')

await page.locator('textarea').fill('how much is a check-up?')
await page.keyboard.press('Enter')
await page.waitForTimeout(9000)

// Greeting, the question, one answer. More than three means a turn was sent twice — the failure
// that turned one spoken sentence into four replies.
const bubbles = await page.locator('.bubble').count()
check('one question gives one reply', bubbles === 3, `${bubbles} bubbles, expected 3`)

console.log(`\nconsole problems: ${problems.length}`)
for (const problem of [...new Set(problems)].slice(0, 15)) {
  console.log(`  ${problem.slice(0, 160)}`)
}

await browser.close()

if (failures || problems.length) {
  console.log(`\n${failures} check(s) failed, ${problems.length} console problem(s)`)
  process.exit(1)
}
console.log('\nall clear')
