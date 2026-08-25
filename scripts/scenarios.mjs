/* What the studio does when things go wrong.
 *
 * Not "does it work" — the smoke test covers that. This is the other half: the gateway is down,
 * the socket drops mid-call, the window is narrow, someone is using a keyboard. Every one of
 * these is a state a user WILL hit, and a blank screen or a silent failure in any of them is a
 * bug the happy path cannot find.
 */
import { chromium } from 'playwright'

const BASE = process.env.STUDIO_URL ?? 'http://localhost:5173'
let failures = 0
const check = (label, ok, detail = '') => {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? `  (${detail})` : ''}`)
  if (!ok) failures += 1
}

const browser = await chromium.launch({
  args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
         '--autoplay-policy=no-user-gesture-required'],
})

// ── the gateway is not running ───────────────────────────────────────────────
{
  console.log('gateway unreachable:')
  const page = await browser.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  // Fail every API call, as if nothing were listening on the port.
  await page.route('**/api/**', (route) => route.abort('connectionrefused'))
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  const text = await page.evaluate(() => document.body.innerText)
  check('the page still renders', text.length > 40, `${text.length} chars`)
  // Specifically: it must SAY SO, and say what to do. With the gateway down this page used to
  // show four loading skeletons indefinitely — the least honest thing a UI can do, because it
  // tells the user to keep waiting for something that is never going to arrive.
  check('it says the gateway is not answering', /not answering/i.test(text))
  check('it says how to fix it', /dialtone serve/i.test(text))
  check(
    'it does not pretend to still be loading',
    !(await page.evaluate(() => document.querySelector('.content')?.innerHTML ?? '')).includes('skeleton'),
  )
  check('no uncaught exception', errors.length === 0, errors[0] ?? '')
  await page.close()
}

// ── the socket drops mid-call ────────────────────────────────────────────────
{
  console.log('\nsocket drops mid-call:')
  const context = await browser.newContext({ permissions: ['microphone'] })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto(BASE, { waitUntil: 'networkidle' })

  await page.getByRole('navigation').getByRole('button', { name: /^Live call/ }).click()
  await page.getByRole('tab', { name: 'Chat' }).click()
  await page.getByRole('button', { name: /Start chat/ }).click()
  await page.waitForTimeout(2500)

  // Take the network away underneath the open socket, which is what a dropped connection is.
  await context.setOffline(true)
  await page.waitForTimeout(2500)

  const text = await page.evaluate(() => document.body.innerText)
  check('the page survives', text.length > 40)
  check('no uncaught exception', errors.length === 0, errors[0] ?? '')
  check('there is still a way out', await page.getByRole('button', { name: /End chat|Start/ }).count() > 0)
  await context.setOffline(false)
  await context.close()
}

// ── a narrow window ──────────────────────────────────────────────────────────
{
  console.log('\nnarrow window (1024px):')
  const page = await browser.newPage({ viewport: { width: 1024, height: 800 } })
  await page.goto(BASE, { waitUntil: 'networkidle' })
  for (const screen of ['Dashboard', 'Live call', 'Appointments']) {
    await page.getByRole('navigation').getByRole('button', { name: new RegExp(`^${screen}`) }).click()
    await page.waitForTimeout(900)
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth)
    check(`${screen} does not scroll sideways`, overflow <= 1, `${overflow}px over`)
  }
  await page.close()
}

// ── keyboard only ────────────────────────────────────────────────────────────
{
  console.log('\nkeyboard:')
  const page = await browser.newPage()
  await page.goto(BASE, { waitUntil: 'networkidle' })

  await page.keyboard.press('Tab')
  const firstFocus = await page.evaluate(() => document.activeElement?.tagName)
  check('tab reaches something', Boolean(firstFocus) && firstFocus !== 'BODY', firstFocus ?? 'none')

  // Every interactive element must show where focus is.
  const invisible = await page.evaluate(() => {
    const targets = [...document.querySelectorAll('button, a, input, select, textarea')]
    let bad = 0
    for (const el of targets.slice(0, 40)) {
      el.focus()
      const style = getComputedStyle(el)
      const ring = style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0
      const shadow = style.boxShadow && style.boxShadow !== 'none'
      if (!ring && !shadow) bad += 1
    }
    return bad
  })
  check('focus is visible', invisible === 0, `${invisible} elements with no focus ring`)
  await page.close()
}

await browser.close()
console.log(failures ? `\n${failures} problems` : '\nall clear')
process.exit(failures ? 1 : 0)
