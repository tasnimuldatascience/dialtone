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
  'Dashboard', 'Live call', 'Appointments', 'Call history', 'Agents', 'Knowledge',
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

console.log('\ncall mode:')
await nav.getByRole('button', { name: 'Live call', exact: true }).click()
await page.getByRole('tab', { name: 'Call' }).click()
await page.waitForTimeout(300)
await page.getByRole('button', { name: /Start call/ }).click()
await page.waitForTimeout(6000)

// Hands-free is the whole promise of this mode. Nobody pressed anything.
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

// The details go in typed. This is the path a real caller takes for anything the recogniser
// would mangle, and it has to reach the agent's memory rather than just sitting in a box.
console.log('\ntyped details:')
await page.locator('.field input').nth(0).fill('Tasnimul Hasan')
await page.locator('.field input').nth(1).fill('(212) 555-0142')
await page.locator('.field input').nth(2).fill('tasnimul@example.com')
await page.getByRole('button', { name: /Save details/ }).click()
await page.waitForTimeout(1200)

const known = await page.locator('.know[data-confirmed="true"]').count()
check('typed details reach the agent', known >= 3, `${known} confirmed facts shown`)
// Not "nothing is unconfirmed" — the REASON is heard from the conversation and shown as
// heard, which is the feature. What must not survive is a name, phone or email the agent only
// thinks it heard, because that is what a booking would be written from.
check('no contact detail is left as a guess',
  !(await page.locator('.panel-note', { hasText: /heard,\s*not typed/ }).count()))

await page.getByRole('button', { name: /Hang up/ }).click()
await page.waitForTimeout(800)

// Chat is the other product, and the microphone means something different in it.
console.log('\nchat mode:')
await page.getByRole('tab', { name: 'Chat' }).click()
await page.getByRole('button', { name: /Start chat/ }).click()
await page.waitForTimeout(3500)

check('the microphone does NOT open itself in chat',
  (await page.locator('.mic[data-on="true"]').count()) === 0)

await page.locator('.mic').click()
await page.waitForTimeout(1500)
check('dictation is visibly not a recording', (await page.locator('.mic[data-dictate="true"][data-on="true"]').count()) > 0)

// A minute of fake-microphone silence must not send anything. In Call mode the endpointer would
// have ended a turn by now; in Chat nothing may leave the box until a person presses send.
const beforeChat = await page.locator('.bubble').count()
await page.waitForTimeout(4000)
check('nothing is sent without pressing send',
  (await page.locator('.bubble').count()) === beforeChat)

await page.locator('textarea').fill('what are your opening hours?')
await page.keyboard.press('Enter')
await page.waitForTimeout(9000)
check('a typed message gets one reply',
  (await page.locator('.bubble').count()) === beforeChat + 2,
  `${await page.locator('.bubble').count()} bubbles`)

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
