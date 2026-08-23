/* Speaking to the agent and hearing it reply.
 *
 * USES THE BROWSER'S OWN SPEECH ENGINES, and that is a considered choice rather than a shortcut.
 * Chrome and Edge ship both recognition and synthesis. Using them means voice works the moment
 * you open the page — no model downloads, no GPU contention with the language model, no second
 * service to run. For a product someone is evaluating, "it works immediately" beats "it is
 * architecturally purer" by a wide margin.
 *
 * The production path is the same shape: `Recognizer` and `Synthesizer` in the gateway are the
 * server-side equivalents, and swapping Whisper and Piper in behind them changes nothing above.
 *
 * WHAT THE BROWSER GETS WRONG, AND WHAT WE DO ABOUT IT:
 *
 *   IT ENDPOINTS FOR YOU, BADLY. Web Speech decides you have finished speaking on its own
 *   schedule and fires `onend`. That is precisely the decision this whole project exists to make
 *   properly. So we run in continuous mode, ignore its endpointing entirely, and feed the
 *   interim transcript to our own endpointer — which is the only way the demo demonstrates the
 *   thing it claims.
 *
 *   IT STOPS WITHOUT ASKING. Recognition ends by itself after a stretch of silence, browser
 *   version depending. If it should still be listening, restart it.
 *
 *   VOICES LOAD LATE. `getVoices()` is empty on first call in Chrome and populates
 *   asynchronously, so choosing a voice at construction time silently gets the default.
 */

export const speechSupported = (): boolean =>
  typeof window !== 'undefined' &&
  ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window)

export const synthSupported = (): boolean =>
  typeof window !== 'undefined' && 'speechSynthesis' in window

type RecognitionCtor = new () => SpeechRecognitionLike

interface SpeechRecognitionLike {
  continuous: boolean
  interimResults: boolean
  lang: string
  start(): void
  stop(): void
  abort(): void
  onresult: ((e: SpeechRecognitionEventLike) => void) | null
  onend: (() => void) | null
  onerror: ((e: { error: string }) => void) | null
}

interface SpeechRecognitionEventLike {
  resultIndex: number
  results: {
    length: number
    [i: number]: { isFinal: boolean; 0: { transcript: string } }
  }
}

export interface ListenerHandlers {
  /** Fires on every interim update. This is what the endpointer reads. */
  onPartial: (text: string) => void
  /** Fires when the browser marks a phrase final. Used as a hint, never as the turn boundary. */
  onFinal?: (text: string) => void
  onError?: (message: string) => void
}

/**
 * True while the agent is speaking out loud.
 *
 * THE ECHO PROBLEM. With speakers on, the microphone hears the agent. Web Speech transcribes it
 * happily, the endpointer treats it as the caller, and the agent starts answering its own
 * sentences — which is exactly what happened the first time this was used with voice on:
 *
 *     agent says:   "Certainly. Prices vary based on the service."
 *     mic hears:    "certainly are prices vary based on the service"
 *     endpointer:   sends that to the agent as if the caller had said it
 *
 * Two defences, because neither is sufficient alone. The browser's own echo cancellation
 * (requested in `MicLevel`) removes most of it on a headset and much less of it on laptop
 * speakers at volume. So recognition results are also DISCARDED while the agent is speaking —
 * half-duplex, which is what a phone line does anyway, and what every hands-free device has done
 * since long before any of this.
 *
 * The cost is that a caller genuinely interrupting mid-sentence is not heard until the agent
 * stops. That is the correct trade for a browser demo: the alternative is an agent that
 * interrupts itself, which is unusable rather than merely limited. On a real telephony path the
 * carrier does echo cancellation and `turn/bargein.py` handles the rest.
 */
let agentSpeaking = false

export const isAgentSpeaking = (): boolean => agentSpeaking

/**
 * Continuous microphone transcription.
 *
 * Deliberately does NOT decide when a turn has ended — it reports words, and the caller decides.
 * That separation is the entire point: turn-taking is the thing being demonstrated, so handing
 * it to the browser would demonstrate the browser.
 */
export class Listener {
  private recognition: SpeechRecognitionLike | null = null
  private wantRunning = false
  private settled = ''

  constructor(private handlers: ListenerHandlers, private lang = 'en-GB') {}

  get running(): boolean {
    return this.wantRunning
  }

  start(): void {
    if (!speechSupported() || this.wantRunning) return
    const Ctor = ((window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor })
      .SpeechRecognition ??
      (window as unknown as { webkitSpeechRecognition?: RecognitionCtor }).webkitSpeechRecognition) as RecognitionCtor

    const recognition = new Ctor()
    recognition.continuous = true
    recognition.interimResults = true
    recognition.lang = this.lang

    recognition.onresult = (event) => {
      // Drop anything heard while the agent is talking: it is the agent, coming back through
      // the microphone. Discarding rather than pausing recognition keeps the engine warm, which
      // matters because restarting it costs a beat of real speech.
      if (agentSpeaking) return

      let interim = ''
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i]
        const text = result[0].transcript
        if (result.isFinal) {
          this.settled = `${this.settled} ${text}`.trim()
          this.handlers.onFinal?.(text.trim())
        } else {
          interim += text
        }
      }
      const combined = `${this.settled} ${interim}`.trim()
      if (combined) this.handlers.onPartial(combined)
    }

    recognition.onerror = (event) => {
      // "no-speech" and "aborted" are routine, not failures: the first is a quiet caller and the
      // second is us stopping it. Surfacing them would fill the screen with false alarms.
      if (event.error !== 'no-speech' && event.error !== 'aborted') {
        this.handlers.onError?.(event.error)
      }
    }

    recognition.onend = () => {
      // The browser stops on its own after a stretch of silence. If we still want to be
      // listening, start it again — otherwise the microphone dies mid-call for no visible reason.
      if (this.wantRunning) {
        try {
          recognition.start()
        } catch {
          /* already restarting; the next onend will try again */
        }
      }
    }

    this.recognition = recognition
    this.wantRunning = true
    try {
      recognition.start()
    } catch (error) {
      this.wantRunning = false
      this.handlers.onError?.(String(error))
    }
  }

  stop(): void {
    this.wantRunning = false
    this.recognition?.stop()
    this.recognition = null
  }

  /** Clear the settled text. Called when a turn is taken, so the next one starts clean. */
  reset(): void {
    this.settled = ''
  }
}

// ── speaking ────────────────────────────────────────────────────────────────
let cachedVoices: SpeechSynthesisVoice[] = []

/**
 * The available voices.
 *
 * Chrome returns an empty list on the first call and populates it asynchronously, so this waits
 * for `voiceschanged` once. Choosing a voice without waiting silently gets the system default,
 * which is how a "voice picker" ends up having no effect at all.
 */
export function loadVoices(): Promise<SpeechSynthesisVoice[]> {
  if (!synthSupported()) return Promise.resolve([])
  const now = speechSynthesis.getVoices()
  if (now.length) {
    cachedVoices = now
    return Promise.resolve(now)
  }
  return new Promise((resolve) => {
    const done = () => {
      cachedVoices = speechSynthesis.getVoices()
      resolve(cachedVoices)
    }
    speechSynthesis.addEventListener('voiceschanged', done, { once: true })
    // Some builds never fire the event when the list is already warm elsewhere.
    setTimeout(done, 900)
  })
}

/** Best match for a named preference, falling back sensibly rather than to nothing. */
export function pickVoice(preference: string): SpeechSynthesisVoice | null {
  if (!cachedVoices.length) cachedVoices = synthSupported() ? speechSynthesis.getVoices() : []
  if (!cachedVoices.length) return null

  const english = cachedVoices.filter((v) => v.lang.startsWith('en'))
  const pool = english.length ? english : cachedVoices
  const wantFemale = preference.includes('female')

  const named = pool.find((v) =>
    wantFemale
      ? /female|samantha|serena|karen|moira|fiona|zira|hazel|sonia/i.test(v.name)
      : /male|daniel|alex|george|ryan|david|arthur/i.test(v.name),
  )
  return named ?? pool[0] ?? null
}

export interface SpeakHandles {
  cancel: () => void
}

/**
 * Say something.
 *
 * `onStart` fires when audio actually begins, which is the number that belongs in a latency
 * budget — the gap between requesting speech and hearing it is real and it is not zero.
 */
export function speak(
  text: string,
  opts: { voice?: string; rate?: number; onStart?: () => void; onEnd?: () => void } = {},
): SpeakHandles {
  if (!synthSupported() || !text.trim()) {
    opts.onEnd?.()
    return { cancel: () => undefined }
  }

  // Anything still queued belongs to the previous turn. Letting it finish would have the agent
  // talking over its own next sentence, which is the exact failure this project is about.
  speechSynthesis.cancel()

  const utterance = new SpeechSynthesisUtterance(text)
  const voice = pickVoice(opts.voice ?? 'female-warm')
  if (voice) utterance.voice = voice
  // Slightly quicker than default: browser TTS reads noticeably slower than a receptionist does.
  utterance.rate = opts.rate ?? 1.06
  utterance.pitch = 1.0
  utterance.onstart = () => {
    agentSpeaking = true
    opts.onStart?.()
  }
  const finish = () => {
    // A short tail after the audio ends: the speakers are still settling and the last syllable
    // reaches the microphone slightly after the engine considers itself done.
    window.setTimeout(() => { agentSpeaking = false }, 250)
    opts.onEnd?.()
  }
  utterance.onend = finish
  utterance.onerror = finish

  speechSynthesis.speak(utterance)
  return { cancel: () => speechSynthesis.cancel() }
}

export function stopSpeaking(): void {
  agentSpeaking = false
  if (synthSupported()) speechSynthesis.cancel()
}

// ── microphone level ────────────────────────────────────────────────────────
/**
 * A live loudness reading for the level meter.
 *
 * Separate from recognition on purpose: Web Speech gives no audio level at all, and a call UI
 * with no visible sign that the microphone is hearing you is impossible to trust. This is the
 * only honest way to show it.
 */
export class MicLevel {
  private context: AudioContext | null = null
  private stream: MediaStream | null = null
  private raf = 0

  async start(onLevel: (level: number) => void): Promise<void> {
    try {
      // Ask the browser for echo cancellation, noise suppression and gain control. It handles
      // most of the speaker bleed on a headset; the half-duplex guard in `Listener` covers what
      // it misses on open laptop speakers.
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
    } catch {
      return                                  // permission refused; the meter simply stays flat
    }
    const AudioCtor = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    this.context = new AudioCtor()
    const source = this.context.createMediaStreamSource(this.stream)
    const analyser = this.context.createAnalyser()
    analyser.fftSize = 512
    source.connect(analyser)

    const buffer = new Uint8Array(analyser.frequencyBinCount)
    const tick = () => {
      analyser.getByteTimeDomainData(buffer)
      let sum = 0
      for (const sample of buffer) {
        const centred = (sample - 128) / 128
        sum += centred * centred
      }
      // RMS, then a gentle curve — raw RMS on speech sits low enough that a linear meter looks
      // broken even when the microphone is working perfectly.
      onLevel(Math.min(1, Math.sqrt(sum / buffer.length) * 3.2))
      this.raf = requestAnimationFrame(tick)
    }
    tick()
  }

  stop(): void {
    cancelAnimationFrame(this.raf)
    this.stream?.getTracks().forEach((t) => t.stop())
    void this.context?.close()
    this.context = null
    this.stream = null
  }
}
