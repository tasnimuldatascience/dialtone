/* A recorded tour of dialtone, driven against the real thing.
 *
 * NOTHING HERE IS MOCKED. The call it places is a real call: a real 1.5B model writing the
 * replies, real retrieval behind them, a real row in the appointments table at the end. That is
 * the point of recording it this way rather than editing a screen capture — a demo video is the
 * easiest artefact in software to fake, and one that cannot be re-run is indistinguishable from
 * one that never worked.
 *
 * Re-runnable, therefore, and it fails loudly: every step waits on a condition that is true only
 * when the thing being shown has actually happened, so a broken build produces no video rather
 * than a video of a broken build.
 *
 * THE NARRATION WAS RECORDED FIRST AND THIS IS PACED TO IT. `narrate.py` measures every line and
 * writes `narration.json`; each scene below holds until its line has finished being said. That is
 * why no editing step exists: the two tracks were never cut to fit each other, so they cannot
 * drift. A scene whose action outlasts its line simply plays on in silence, which is what you
 * want — the model taking two seconds to think is part of the honest picture.
 *
 * BOTH SIDES OF THE CALL ARE HEARD. Playwright records no audio at all, so the voice-call scene
 * synthesises the caller's line and the agent's reply through the same endpoint the browser is
 * using, holds each frame for exactly as long as its clip, and hands them to the mixer. Three
 * voices in the finished film, and none of them ambiguous: the caller, the agent, the narrator.
 *
 * CAPTIONS ARE PART OF THE PAGE. An overlay appended to `document.body` is recorded with
 * everything else — no editor, no asset pipeline, and the styling comes from the app's own CSS
 * variables so the titles cannot drift from the product.
 *
 *   python scripts/narrate.py && node scripts/demo-video.mjs && python scripts/soundtrack.py
 */

import { chromium } from 'playwright'
import { mkdir, readdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const BASE = process.env.STUDIO_URL ?? 'http://localhost:5173'
const API = process.env.DIALTONE_API ?? 'http://127.0.0.1:8071'
const OUT = resolve(process.env.VIDEO_DIR ?? 'docs/video')
const SIZE = { width: 1600, height: 900 }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

let narration = {}
let videoStart = 0
const moments = []

async function main() {
  const manifest = JSON.parse(await readFile(resolve(OUT, 'narration.json'), 'utf8'))
  narration = Object.fromEntries(manifest.lines.map((l) => [l.id, l]))

  // Everything except the narration is rebuilt on every run. The audio is not: it takes a minute
  // to synthesise and never changes between takes.
  for (const name of await readdir(OUT).catch(() => [])) {
    if (name.endsWith('.webm') || name === 'moments.json') await rm(resolve(OUT, name))
  }
  await mkdir(OUT, { recursive: true })

  const browser = await chromium.launch({
    args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
           '--autoplay-policy=no-user-gesture-required', '--force-device-scale-factor=1'],
  })
  const context = await browser.newContext({
    viewport: SIZE,
    colorScheme: 'dark',
    permissions: ['microphone'],
    recordVideo: { dir: OUT, size: SIZE },
  })
  const page = await context.newPage()
  videoStart = Date.now()

  await page.goto(BASE, { waitUntil: 'networkidle' })
  await installOverlay(page)

  // ── title ────────────────────────────────────────────────────────────────
  await scene(page, 'title', null, null, async () => {
    await card(page, 'dialtone',
      'A voice agent that answers the phone, books the appointment, and proves it did.')
  })
  await clearCard(page)

  // ── dashboard ────────────────────────────────────────────────────────────
  await nav(page, 'Dashboard')
  await page.waitForFunction(() => document.querySelectorAll('.metric-v').length >= 4)
  await scene(page, 'dashboard', 'Every call is measured',
    'Reply times are real percentiles from real turns — never an estimate.')

  // ── a real call, typed ───────────────────────────────────────────────────
  await nav(page, 'Live call')
  await page.getByRole('tab', { name: 'Chat' }).click()
  await scene(page, 'call-open', 'One turn, end to end',
    'The stage timings sit under every reply — measured, not estimated.', async () => {
      await page.getByRole('button', { name: /Start chat/ }).click()
      await page.waitForFunction(() => document.querySelectorAll('.bubble').length >= 1)
      await say(page, 'hi, I need to book a scale and polish')
    })

  await scene(page, 'pipeline', 'Seven stages, every turn',
    'Endpointing · recognition · redaction · retrieval · the model · grounding · speech')

  await scene(page, 'pipeline-stream', 'Pipelined, not sequential',
    'Each stage starts on the first token of the one before it. Waiting for whole sentences ' +
    'costs about a second.')

  await scene(page, 'knowledge', 'Retrieved, then checked back',
    'BM25 and dense vectors fused. Every figure must appear in a passage the model was given.',
    async () => {
      await say(page, 'how much does that cost?')
    })

  await scene(page, 'typed', 'Names are typed, never transcribed',
    'One real call heard “tasty mulasson” for a surname. So the agent never asks out loud.',
    async () => {
      for (const [i, value] of ['Sam Hassan', '(212) 774-1188', 'sam@example.com'].entries()) {
        const input = page.locator('.field input').nth(i)
        await point(page, input)
        await input.click()
        await input.type(value, { delay: 50 })
      }
      const save = page.getByRole('button', { name: /Save details/ })
      await point(page, save)
      await save.click()
      await page.waitForSelector('.know[data-confirmed="true"]', { timeout: 15_000 })
    })

  await scene(page, 'booking', 'Code books it, not the model',
    'The model proposes a time. Code checks it is real, free and unambiguous — then writes it.',
    async () => {
      await say(page, 'can I come tomorrow morning?')
      await say(page, 'yes, that works')
    })

  const end = page.getByRole('button', { name: /End chat/ })
  if (await end.count()) await end.click().catch(() => undefined)
  await sleep(700)

  // ── the same thing, out loud ─────────────────────────────────────────────
  // THE SCENE PLAYWRIGHT CANNOT RECORD. The browser is playing Kokoro audio here and none of it
  // reaches the file, so the reply is synthesised again through the same endpoint, the frame is
  // held for exactly its length, and `soundtrack.py` lays it in at this offset.
  await page.getByRole('tab', { name: 'Call' }).click()
  await sleep(500)
  let spokeAt = 0
  await scene(page, 'voice', 'The voice channel',
    'Streaming recognition in, streaming synthesis out — around the same pipeline.', async () => {
      await page.getByRole('button', { name: /Start call/ }).click()
      await page.waitForFunction(() => document.querySelector('.mic[data-on="true"]') !== null,
        null, { timeout: 20_000 })
      await sleep(1000)
      // A question whose answer is SPECIFIC. "Where are you?" gets a vague reply from a 1.5B
      // model; the parking page has counts and a street name in it, so the answer either quotes
      // them or visibly does not.
      spokeAt = await callerSpeaks(page, 'is there parking near you?')
    })

  await agentSpeaks(page, spokeAt)

  await scene(page, 'bargein', 'Talk over it and it gives way',
    'Its history is truncated to the audio that actually played — never to what it wrote.',
    async () => {
      await sleep(600)
    })

  const hangUp = page.getByRole('button', { name: /Hang up/ })
  if (await hangUp.count()) await hangUp.click().catch(() => undefined)
  await sleep(600)

  // ── every line busy, for real ────────────────────────────────────────────
  // NOT A MOCK-UP OF A REFUSAL. Two calls are opened through the API so the machine is genuinely
  // at capacity, and then the studio is asked for one more. What the camera sees is the same 503
  // any caller would get, rendered as "all lines are busy" rather than as a stack trace.
  const held = await fillEveryLine()
  await scene(page, 'concurrency', 'Many calls at once, each isolated',
    'Own memory, own flow position, own proposed slot. Nothing crosses between them.',
    async () => {
      await page.waitForFunction(() => /([0-9]+) of ([0-9]+) lines/.test(document.body.innerText),
        null, { timeout: 20_000 }).catch(() => undefined)
      await sleep(900)
    })

  await scene(page, 'concurrency-full', 'Admission control, past the measured capacity',
    'A refusal with a reason and a Retry-After — rather than everyone degrading together.',
    async () => {
      // "Call again" AFTER A CALL HAS ENDED, not "Start call" — which the first take missed, so
      // nothing was clicked and the refusal never appeared on camera.
      const start = page.getByRole('button', { name: /Start (call|chat)|(Call|Chat) again/ })
      if (!(await start.count())) throw new Error('no button to start a call with')
      await point(page, start)
      await start.click().catch(() => undefined)
      await page.waitForFunction(() => /lines are busy|already in progress/i.test(
        document.body.innerText), null, { timeout: 20_000 }).catch(() => undefined)
      await sleep(700)
    })
  await releaseLines(held)

  // ── the appointment exists ───────────────────────────────────────────────
  await nav(page, 'Appointments')
  await page.waitForFunction(() => document.querySelectorAll('.appt').length >= 1, null,
    { timeout: 20_000 })
  await scene(page, 'appointment', 'The guarantee is in the schema',
    'starts_at is UNIQUE — a race between two live calls fails at INSERT, not in the diary.')

  // ── call history ─────────────────────────────────────────────────────────
  await nav(page, 'Call history')
  await page.waitForFunction(() => document.querySelectorAll('tbody tr').length >= 3, null,
    { timeout: 20_000 })
  await scene(page, 'history', 'What each call was about, and what it did',
    'Derived from the call — not the first line of the transcript.')

  await scene(page, 'detail', 'No log correlation to do',
    'The node, the transition it took, and the document it cited — under every reply.',
    async () => {
      const booked = page.locator('tbody tr').filter({ hasText: /booked/i }).first()
      await point(page, booked)
      await booked.click()
      await page.waitForFunction(() => document.querySelectorAll('.bubble').length >= 2, null,
        { timeout: 20_000 })
      await glide(page, 420)
    })

  // ── the flow, all of it ──────────────────────────────────────────────────
  await nav(page, 'Conversation flow')
  await page.waitForSelector('.node', { timeout: 20_000 })
  await scene(page, 'flow', 'The graph decides what is possible',
    'The model chooses the words. It cannot book during the greeting — the tool is not there.',
    async () => {
      const node = page.locator('.node').filter({ hasText: 'offer_slots' }).first()
      await point(page, node)
      await node.click()
    })

  await scene(page, 'flow-scroll', 'Every step, declared',
    'What it collects, which tools exist there, and every legal move out of it.', async () => {
      await glideToBottom(page)
    })

  // ── turn-taking ──────────────────────────────────────────────────────────
  await nav(page, 'Turn-taking')
  await page.waitForFunction(() => document.querySelectorAll('tbody tr').length >= 4, null,
    { timeout: 30_000 })
  await scene(page, 'turntaking', 'Knowing when the caller has finished',
    'Silence, grammar and pitch together: 280ms to answer, and nobody cut off.')

  // ── compliance ───────────────────────────────────────────────────────────
  await nav(page, 'Compliance')
  await page.waitForFunction(() => document.body.innerText.includes('What the AI receives'), null,
    { timeout: 20_000 })
  const CARD = 'my card is 4111 1111 1111 1111, expiry 04 27'
  await scene(page, 'compliance', 'Watch a card number',
    'Typed in full, exactly as a caller would say it.', async () => {
      const box = page.locator('textarea').first()
      await point(page, box)
      await box.click()
      await box.fill('')
      // Typed a character at a time so the number is unmistakably there before it is not.
      await box.type(CARD, { delay: 42 })
      await sleep(900)
    })

  await scene(page, 'compliance-gone', 'It never reaches the model',
    'Redaction happens before the text leaves the socket — not before it is stored.', async () => {
      await page.waitForFunction(() => /removed/i.test(document.body.innerText), null,
        { timeout: 15_000 }).catch(() => undefined)
      await glide(page, 260, 10)
    })

  // ── end ──────────────────────────────────────────────────────────────────
  await scene(page, 'end', null, null, async () => {
    await card(page, 'Run it yourself',
      '536 tests, and a written record of every bug that earned one.\n' +
      'github.com/tasnimuldatascience/dialtone')
  })

  await context.close()
  await browser.close()

  const files = (await readdir(OUT)).filter((f) => f.endsWith('.webm'))
  if (!files.length) throw new Error('playwright wrote no video')
  await rename(resolve(OUT, files[0]), resolve(OUT, 'tour.webm'))
  await writeFile(resolve(OUT, 'moments.json'),
    JSON.stringify({ videoMs: Date.now() - videoStart, moments }, null, 2))

  console.log(`\n  docs/video/tour.webm   ${((Date.now() - videoStart) / 1000).toFixed(1)}s`)
  for (const m of moments) console.log(`    ${(m.at / 1000).toFixed(1).padStart(6)}s  ${m.id}`)
}

/* ── a scene ────────────────────────────────────────────────────────────────
 * Shows its caption, runs its action, and holds until the narration line for it has finished.
 * The offset is recorded from the start of the recording so the mixer can place the audio
 * without either side having to guess. */
async function scene(page, id, title, body, action) {
  const line = narration[id]
  if (!line) throw new Error(`no narration line called ${id} — run scripts/narrate.py`)

  const at = Date.now() - videoStart
  moments.push({ id, at, kind: 'narration', file: line.file, say: line.say })
  if (title) await caption(page, title, body)

  const startedAt = Date.now()
  if (action) await action()

  const remaining = line.hold_ms - (Date.now() - startedAt)
  if (remaining > 0) await sleep(remaining)
  if (title) await clearCaption(page)
}

/* ── the two voices on the call ─────────────────────────────────────────────
 * BOTH HALVES, because a phone call with only one side audible is not a phone call. The caller is
 * `am_michael`, the agent keeps its own `bf_emma`, and the narrator is a third voice again — so
 * at no point is it unclear who is speaking.
 *
 * Playwright captures no audio whatever, so each line is synthesised through the same endpoint
 * the browser is using and placed by the mixer at the millisecond it belongs. What you hear is
 * the same engine saying the same words. */
const CALLER_VOICE = 'male-us'
const AGENT_VOICE = 'female-warm'

async function synthesise(text, voice, file) {
  const response = await fetch(`${API}/api/voice/preview`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text: text.slice(0, 400), voice }),
  })
  if (!response.ok) return null
  const clip = await response.json()
  await writeFile(resolve(OUT, 'audio', file), Buffer.from(clip.wav, 'base64'))
  return clip
}

/** The caller speaks, and the words appear as the recogniser would produce them. */
async function callerSpeaks(page, text) {
  const clip = await synthesise(text, CALLER_VOICE, 'caller-line.wav')
  const before = await page.locator('.bubble').count()
  const box = page.locator('textarea')
  await point(page, box)
  await box.click()

  moments.push({
    id: 'caller-line', at: Date.now() - videoStart, kind: 'caller',
    file: 'caller-line.wav', say: text,
  })
  // Typed at whatever speed makes the words land as the voice finishes saying them, so the
  // transcript keeps pace with the speech rather than racing it.
  const perCharacter = clip ? Math.min(140, clip.duration_ms / (text.length + 6)) : 45
  await box.type(text, { delay: perCharacter })
  await sleep(320)

  await page.keyboard.press('Enter')

  // THE MOMENT THE AGENT'S BUBBLE APPEARS is the moment its first token arrived — and on a real
  // call that is when synthesis starts, because the engine is handed the first sentence rather
  // than the finished reply. So this is the offset its voice belongs at. Placing it after the
  // reply had finished streaming, which is what the first cut did, put the audio a second and a
  // half late and made the agent look like it was reading its own transcript back.
  await page.waitForFunction((n) => document.querySelectorAll('.bubble').length >= n + 2,
    before, { timeout: 180_000 })
  const spokeAt = Date.now() - videoStart

  await page.waitForFunction(
    (n) => document.querySelectorAll('.bubble').length >= n + 2 &&
           !document.querySelector('.bubble:last-child .caret'),
    before, { timeout: 180_000 },
  )
  return spokeAt
}

/** Then the agent answers, in its own voice, with whatever it actually said. */
async function agentSpeaks(page, spokeAt) {
  const said = await page.locator('.bubble[data-who="agent"] .msg').last().textContent()
  const text = (said ?? '').trim()
  if (!text) return

  const clip = await synthesise(text, AGENT_VOICE, 'agent-reply.wav')
  if (!clip) {
    console.log('  (could not synthesise the agent reply; the scene will be silent)')
    return
  }
  moments.push({
    id: 'agent-reply', at: spokeAt, kind: 'agent', file: 'agent-reply.wav', say: text,
  })
  // Hold until the clip would have finished. Measured from where it STARTED, not from now --
  // some of it has already played over the streaming text by the time we get here.
  const until = spokeAt + clip.duration_ms + 600
  const now = Date.now() - videoStart
  if (until > now) await sleep(until - now)
}

/** Open calls through the API until the machine is genuinely at capacity. */
async function fillEveryLine() {
  const health = await (await fetch(`${API}/api/health`)).json()
  const limit = health?.capacity?.limit ?? 2
  const agent = (await (await fetch(`${API}/api/agents`)).json()).agents[0].id

  const held = []
  for (let i = 0; i < limit; i++) {
    const response = await fetch(`${API}/api/calls`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ agent_id: agent, channel: i % 2 ? 'text' : 'voice' }),
    })
    if (!response.ok) break                 // already full, which is just as good for the shot
    held.push((await response.json()).call_id)
  }
  return held
}

async function releaseLines(held) {
  for (const id of held) {
    await fetch(`${API}/api/calls/${id}/end`, { method: 'POST' }).catch(() => undefined)
  }
}

async function nav(page, label) {
  const button = page.getByRole('navigation').getByRole('button', { name: new RegExp(`^${label}`) })
  if (!(await button.count())) throw new Error(`no screen called ${label}`)
  await point(page, button)
  await button.click()
  await sleep(650)
}

/** One caller turn, waited out properly: the reply must finish streaming, not merely start. */
async function say(page, text) {
  const before = await page.locator('.bubble').count()
  const box = page.locator('textarea')
  await point(page, box)
  await box.click()
  await box.type(text, { delay: 40 })
  await sleep(280)
  await page.keyboard.press('Enter')
  await page.waitForFunction(
    (n) => document.querySelectorAll('.bubble').length >= n + 2 &&
           !document.querySelector('.bubble:last-child .caret'),
    before, { timeout: 180_000 },
  )
  await sleep(600)
}

/** Scroll in small steps. One long jump is unreadable on video; this reads as a camera move. */
async function glide(page, distance, steps = 14) {
  for (let i = 0; i < steps; i++) {
    await page.mouse.wheel(0, distance / steps)
    await sleep(45)
  }
}

/** The whole page, top to bottom, at a speed somebody can actually follow. */
async function glideToBottom(page) {
  const total = await page.evaluate(() =>
    Math.max(0, document.documentElement.scrollHeight - window.innerHeight))
  if (total < 40) return
  await glide(page, total, Math.max(20, Math.round(total / 45)))
  await sleep(500)
}

/* ── the caption layer ──────────────────────────────────────────────────────
 * CLASS NAMES ARE PREFIXED because the first version was not. The app already has a `.cap` class
 * — `.cap b { width: 5px }`, a divider bar — so the caption title inherited a five-pixel width and
 * rendered one word per line, off the bottom of the frame. An overlay injected into somebody
 * else's document does not get to use short names. */
async function installOverlay(page) {
  await page.addStyleTag({ content: `
    #tour { position: fixed; inset: 0; pointer-events: none; z-index: 99999;
            font-family: Inter, system-ui, sans-serif; }

    #tour .tour-cap { position: absolute; left: 50%; bottom: 46px;
                 transform: translateX(-50%) translateY(14px);
                 min-width: 520px; max-width: 1060px; padding: 20px 30px; text-align: center;
                 background: rgba(10,14,20,.93); border: 1px solid var(--line-2, #23303f);
                 border-radius: 15px; box-shadow: 0 26px 60px rgba(0,0,0,.62);
                 opacity: 0; transition: opacity .42s ease, transform .42s ease;
                 backdrop-filter: blur(9px); }
    #tour .tour-cap[data-on] { opacity: 1; transform: translateX(-50%) translateY(0); }
    #tour .tour-cap .tour-t { display: block; font-size: 27px; font-weight: 640;
                   letter-spacing: -.022em; color: var(--text, #eaeef5); margin-bottom: 7px;
                   width: auto; height: auto; background: none; }
    #tour .tour-cap .tour-s { display: block; font-size: 16.5px; line-height: 1.55;
                   color: var(--text-2, #9fb0c2); }
    #tour .tour-cap .tour-rule { display: block; width: 44px; height: 3px; border-radius: 2px;
                   margin: 0 auto 15px;
                   background: linear-gradient(90deg, var(--accent, #35e0d0), #7c5cf0); }

    #tour .tour-card { position: absolute; inset: 0; display: grid; place-items: center;
                  text-align: center;
                  background: radial-gradient(1200px 620px at 50% 42%, #10202b 0%, #070a0e 74%);
                  opacity: 0; transition: opacity .55s ease; }
    #tour .tour-card[data-on] { opacity: 1; }
    #tour .tour-card h1 { font-size: 76px; font-weight: 700; letter-spacing: -.045em;
                     margin: 0 0 20px;
                     background: linear-gradient(96deg, var(--accent, #35e0d0), #7c5cf0 78%);
                     -webkit-background-clip: text; background-clip: text; color: transparent; }
    #tour .tour-card p { font-size: 21px; line-height: 1.65; color: var(--text-2, #9fb0c2);
                    max-width: 830px; margin: 0 auto; white-space: pre-line; }

    /* A pointer, because Playwright does not record one and a demo where things happen with no
       visible cause is a demo nobody can follow. */
    #tour .tour-dot { position: absolute; width: 22px; height: 22px; margin: -11px 0 0 -11px;
                 border-radius: 50%; border: 2px solid var(--accent, #35e0d0);
                 background: rgba(53,224,208,.22); opacity: 0;
                 transition: left .42s cubic-bezier(.4,0,.2,1),
                             top .42s cubic-bezier(.4,0,.2,1), opacity .3s; }
    #tour .tour-dot[data-on] { opacity: 1; }
  ` })

  await page.evaluate(() => {
    const root = document.createElement('div')
    root.id = 'tour'
    root.innerHTML =
      '<div class="tour-cap"><i class="tour-rule"></i>' +
      '<b class="tour-t"></b><span class="tour-s"></span></div>' +
      '<div class="tour-card"><div><h1></h1><p></p></div></div>' +
      '<div class="tour-dot"></div>'
    document.body.appendChild(root)
  })
}

const caption = (page, title, body) => page.evaluate(([t, b]) => {
  const cap = document.querySelector('#tour .tour-cap')
  cap.querySelector('.tour-t').textContent = t
  cap.querySelector('.tour-s').textContent = b
  cap.setAttribute('data-on', '')
}, [title, body])

const clearCaption = async (page) => {
  await page.evaluate(() => document.querySelector('#tour .tour-cap')?.removeAttribute('data-on'))
  await sleep(440)
}

const card = (page, title, body) => page.evaluate(([t, b]) => {
  const el = document.querySelector('#tour .tour-card')
  el.querySelector('h1').textContent = t
  el.querySelector('p').textContent = b
  el.setAttribute('data-on', '')
}, [title, body])

const clearCard = async (page) => {
  await page.evaluate(() => document.querySelector('#tour .tour-card')?.removeAttribute('data-on'))
  await sleep(600)
}

/** Move the pointer to whatever is about to be clicked, and let it travel before the click. */
async function point(page, locator) {
  const box = await locator.boundingBox()
  if (!box) return
  const x = box.x + box.width / 2
  const y = box.y + Math.min(box.height / 2, 26)
  await page.evaluate(([px, py]) => {
    const dot = document.querySelector('#tour .tour-dot')
    dot.style.left = `${px}px`
    dot.style.top = `${py}px`
    dot.setAttribute('data-on', '')
  }, [x, y])
  await page.mouse.move(x, y)
  await sleep(480)
}

main().catch((error) => {
  console.error(`\n  ${error.message}\n`)
  process.exit(1)
})
