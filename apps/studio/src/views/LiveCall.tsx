import { useCallback, useEffect, useRef, useState } from 'react'
import type { ViewProps } from '../App'
import { api, openCall, type Booked, type CallMemory, type Grounding, type Hit, type IntakeField, type Slot, type Timing } from '../api'
import { Icon } from '../components/Icon'
import { AudioQueue, Listener, MicLevel, clearSpokenMemory, inEchoWindow, loadVoices, looksLikeEcho, rememberSpoken, resetEchoWindow, setAgentAudioProbe, speak, speechSupported, stopSpeaking, synthSupported } from '../voice'
import { decideTurn } from '../turntaking'
import { trackBargeIn } from '../bargein'
import { endsOnFiller, polish } from '../transcript'

/* Talking to the agent, by typing or by voice.
 *
 * TWO THINGS ARE HAPPENING AT ONCE and the screen has to show both: the conversation, and the
 * machinery underneath it. A chat window alone would hide everything this product is actually
 * selling — where the time went, which document the answer came from, whether the number the
 * agent just said exists anywhere in the company's own documents.
 *
 * VOICE USES THE BROWSER'S ENGINES but NOT its turn-taking. Web Speech will happily tell you
 * when it thinks the caller stopped; that decision is the whole subject of this project, so the
 * transcript is streamed to our own endpointer and the browser's opinion is ignored.
 *
 * TWO MODES, AND THE MICROPHONE MEANS SOMETHING DIFFERENT IN EACH. They are not a preference
 * toggle over one behaviour — they are two products, and conflating them is what made the
 * microphone feel broken:
 *
 *   CALL   Hands-free. The agent speaks, the microphone stays open, and the endpointer decides
 *          when the caller has finished. Nobody presses anything. Every hard problem in this
 *          repo lives here.
 *   CHAT   Typed. The agent stays silent. The microphone is DICTATION ONLY: it fills the box and
 *          stops there, and you press send. A half-heard sentence costs a backspace instead of
 *          a wasted turn, which is the entire reason the two are separate.
 *
 * NAMES, PHONE NUMBERS AND EMAIL ADDRESSES ARE TYPED IN BOTH. Recognition mangles precisely the
 * values that have to be exact — one real call produced "tasty mulasson" for a surname. The form
 * on the right is not a fallback for when the voice fails; it is the only path those fields ever
 * take, and a typed value outranks anything the agent thinks it heard.
 */

type Mode = 'call' | 'chat'

interface Line {
  who: 'caller' | 'agent'
  text: string
  streaming?: boolean
  timing?: Timing
  citations?: Hit[]
  grounding?: Grounding
  redacted?: string[]
  refused?: string
  tools?: { name: string; ok: boolean; ms: number }[]
  /** The caller talked over this reply. Only the part that played was kept. */
  interrupted?: boolean
}

type Phase = 'idle' | 'connecting' | 'live' | 'ended'



export function LiveCall({ agent, agents, agentId, setAgentId, ready }: ViewProps) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [mode, setMode] = useState<Mode>('call')
  const [callId, setCallId] = useState('')
  const [lines, setLines] = useState<Line[]>([])
  const [draft, setDraft] = useState('')
  const [thinking, setThinking] = useState(false)
  const [memory, setMemory] = useState<CallMemory | null>(null)
  const [booked, setBooked] = useState<Booked | null>(null)
  const [micOn, setMicOn] = useState(false)
  const [level, setLevel] = useState(0)
  const [partial, setPartial] = useState('')
  const [endpointMs, setEndpointMs] = useState<number | null>(null)
  const [firstToken, setFirstToken] = useState<number | null>(null)
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null)
  const [agentSpeaking, setAgentSpeaking] = useState(false)
  const [silenceMs, setSilenceMs] = useState(0)
  const [turnReason, setTurnReason] = useState('')
  const [interrupted, setInterrupted] = useState(false)
  const [voiceEngine, setVoiceEngine] = useState<'kokoro-82m' | 'browser'>('browser')
  const [firstAudioMs, setFirstAudioMs] = useState<number | null>(null)

  // Not a setting. In Chat the agent is silent and the microphone only ever fills the box, so
  // there is nothing left for a "speak replies" switch to mean.
  const voiceOn = mode === 'call'

  const socket = useRef<ReturnType<typeof openCall> | null>(null)
  const listener = useRef<Listener | null>(null)
  const meter = useRef<MicLevel | null>(null)
  const body = useRef<HTMLDivElement>(null)
  const turnStart = useRef(0)
  const silenceTimer = useRef(0)
  const lastPartial = useRef('')
  // A ref, not the state value. The socket handler is created once and closes over whatever
  // `firstToken` was at that moment, so reading the state inside it measured the previous turn --
  // which is how "to first word" came out LARGER than the whole turn it belonged to.
  const firstTokenAt = useRef<number | null>(null)
  const audio = useRef<AudioQueue | null>(null)
  // A ref as well as state. `start` reads this when the call opens, and the health check that
  // sets it is async -- so a call started before it resolved captured 'browser', spoke the
  // greeting with the robotic voice, and fed it straight back into the microphone.
  const engine = useRef<'kokoro-82m' | 'browser'>('browser')
  const greetingGuard = useRef(0)
  // Last threshold the gateway gave us, and when. Speech recognition emits a partial per word,
  // so scoring every one meant a network round trip per syllable -- several a second, each one
  // delaying the timer it was supposed to be arming.
  const lastScore = useRef({ at: 0, ms: 700 })
  //: When the microphone last heard actual speech. THE turn-taking signal.
  //:
  //: An earlier version watched the TRANSCRIPT instead: arm a timer on each partial, fire if no
  //: new partial arrived. That is not silence detection, and it cut people off mid-sentence --
  //: Web Speech delivers interim results in bursts and goes quiet for a second while it
  //: processes, which reads as a finished turn when it is nothing of the kind.
  //:
  //: The real endpointer works on `silence_ms` from voice-activity frames, and so does this now.
  //: The level meter was already measuring exactly the right thing.
  const lastVoiceAt = useRef(0)
  const speechSeen = useRef(false)
  //: When the transcript last changed. The microphone level is real time; the TRANSCRIPT is not
  //: -- Web Speech delivers words 300-500ms after they were spoken. Ending a turn on audio
  //: silence alone therefore sends a sentence the recogniser has not finished writing down.
  const lastPartialChangeAt = useRef(0)
  //: What has already been dispatched. Web Speech's interim result always contains the whole
  //: phrase from the beginning, so without this every fire re-sends everything said so far --
  //: which is how one spoken sentence became four turns and four replies.
  const sentSoFar = useRef('')
  //: When the current run of loud microphone frames began, while the agent is speaking. Owned
  //: here and passed through `trackBargeIn`, which is a pure function so it can be tested
  //: against timing traces rather than a live microphone.
  const loudSince = useRef(0)
  //: Latched for the duration of one interruption. The level callback fires many times a second,
  //: and without this a single interruption sends a burst of messages, each truncating the
  //: agent's last turn to less than the one before.
  const bargedIn = useRef(false)

  useEffect(() => { void loadVoices() }, [])

  // Which voice engine is actually available. The neural one is better and needs the gateway;
  // the browser one always works. Reported rather than assumed, so the UI can say which you are
  // hearing instead of leaving you to guess why it sounds different.
  useEffect(() => {
    api.health()
      .then((h) => {
        const which = h.voice?.ready ? 'kokoro-82m' : 'browser'
        engine.current = which
        setVoiceEngine(which)
      })
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    const queue = new AudioQueue(() => {
      queue.markDone()
      setAgentSpeaking(false)
    })
    audio.current = queue
    // The listener asks the audio clock directly rather than trusting a flag we maintain.
    setAgentAudioProbe(() => queue.isAudible())
    return () => {
      setAgentAudioProbe(null)
      queue.close()
    }
  }, [])

  // Scroll the transcript container, never the document: scrollIntoView walks up to the page and
  // yanks the whole layout down as tokens arrive.
  useEffect(() => {
    const box = body.current
    if (box) box.scrollTop = box.scrollHeight
  }, [lines, partial])

  const teardown = useCallback(() => {
    socket.current?.close()
    socket.current = null
    listener.current?.stop()
    listener.current = null
    meter.current?.stop()
    meter.current = null
    stopSpeaking()
    window.clearTimeout(silenceTimer.current)
    window.clearTimeout(greetingGuard.current)
  }, [])

  useEffect(() => teardown, [teardown])

  const send = useCallback((text: string) => {
    const clean = text.trim()
    if (!clean || !socket.current) return
    window.clearTimeout(silenceTimer.current)
    setPartial('')
    lastPartial.current = ''
    listener.current?.reset()
    setLines((c) => [...c, { who: 'caller', text: clean }])
    setThinking(true)
    turnStart.current = performance.now()
    firstTokenAt.current = null
    setFirstToken(null)
    socket.current.say(clean)
  }, [])

  /* THE ENDPOINTER, in the browser.
   *
   * Keeps the threshold for the current transcript up to date. It does not decide anything --
   * the decision is made by the silence loop below, against real audio. "my account number is
   * four two" buys ~1.6 seconds of patience; a finished question buys ~250ms.
   */
  const rescore = useCallback(async (text: string) => {
    if (!text.trim()) return
    // Throttled: recognition emits a partial per word, and the threshold barely moves between
    // two consecutive words. A stale value for 200ms is a far smaller error than a network round
    // trip per syllable.
    if (performance.now() - lastScore.current.at < 220) return
    try {
      const verdict = await api.benchScore(text)
      lastScore.current = { at: performance.now(), ms: verdict.threshold_ms }
      setEndpointMs(verdict.threshold_ms)
    } catch {
      /* keep the previous threshold; the fixed fallback is what this project improves on */
    }
  }, [])

  const stopMic = useCallback(() => {
    listener.current?.stop()
    listener.current = null
    meter.current?.stop()
    meter.current = null
    setMicOn(false)
    setLevel(0)
  }, [])

  const startMic = useCallback(async () => {
    if (listener.current) return
    listener.current = new Listener({
      onPartial: (text) => {
        // Anything arriving while the agent is audible is the agent. The listener already drops
        // these, but the check is repeated here because this callback is also what advances the
        // turn state -- and a single leaked frame resets the settle timer, which is enough to
        // stop a turn ever ending.
        if (inEchoWindow()) return

        // New words are proof of speech, independent of the level meter. Microphone gain varies
        // enormously between machines, and a level threshold tuned on one laptop is wrong on the
        // next; this makes the silence detector robust to that without needing calibration.
        if (text !== lastPartial.current) {
          lastVoiceAt.current = performance.now()
          lastPartialChangeAt.current = performance.now()
          speechSeen.current = true
        }
        lastPartial.current = text
        setPartial(text)
        void rescore(text)
      },
    })
    listener.current.start()

    meter.current = new MicLevel()
    void meter.current.start((value) => {
      setLevel(value)

      /* BARGE-IN. Decided from THIS stream, which was opened with echo cancellation, and never
       * from the transcript -- Web Speech opens its own stream that we cannot configure, so
       * while the agent is talking its words are partly the agent's own. */
      const barge = trackBargeIn({
        agentAudible: audio.current?.isAudible(0) ?? false,
        level: value,
        now: performance.now(),
        loudSince: loudSince.current,
        alreadyInterrupted: bargedIn.current,
      })
      loudSince.current = barge.loudSince
      if (barge.reason) setTurnReason(barge.reason)
      if (barge.interrupt) {
        bargedIn.current = true
        // What the caller ACTUALLY heard, read off the audio clock, before the audio is stopped
        // and that information is gone.
        const heard = audio.current?.heardSoFar() ?? ''
        audio.current?.stop()
        // Believe the microphone again immediately. The guard exists because the recogniser
        // reports what it heard several hundred milliseconds late, but the caller is talking RIGHT
        // NOW and holding it shut for another 900ms would swallow the first half of what they
        // said. The content check (`looksLikeEcho`) still catches the agent's own tail words.
        resetEchoWindow()
        setAgentSpeaking(false)
        setInterrupted(true)
        socket.current?.interrupt(heard)
        // The recogniser's buffer is full of the agent. Clear it, or the caller's interruption
        // arrives with the agent's half-sentence stuck to the front of it.
        listener.current?.reset()
        lastPartial.current = ''
        sentSoFar.current = ''
        setPartial('')
        window.setTimeout(() => { bargedIn.current = false; setInterrupted(false) }, 1500)
      }

      // Voice activity. The floor is well above room noise and well below speech; a laptop
      // microphone in a quiet room idles around 0.02.
      //
      // Ignored entirely while the agent is audible: on open speakers the microphone hears the
      // agent loudly, and counting that as caller speech kept resetting the silence timer, so
      // the turn could not end while the agent was talking OR for a while afterwards.
      if (value > 0.08 && !inEchoWindow()) {
        lastVoiceAt.current = performance.now()
        speechSeen.current = true
      }
    })
    setMicOn(true)
  }, [rescore])

  /* DICTATION. The microphone in Chat mode, and nothing else.
   *
   * It writes into the box and stops. It does not decide when you have finished, it does not
   * send, and it cannot start a turn — so a sentence the recogniser mangles costs a backspace
   * rather than a wasted exchange with the agent. That is the whole difference between the two
   * modes, and the reason the endpointer is not wired up here: there is nothing for it to
   * decide when a human is going to press send.
   */
  const startDictation = useCallback(async () => {
    if (listener.current) return
    listener.current = new Listener({
      // Fillers stripped as they arrive, and "comma" written as one. In a call they are
      // load-bearing — "um" is what tells the endpointer somebody is mid-thought — but in a box
      // being typed into they are just noise the caller has to delete. Spoken punctuation is a
      // dictation feature and is enabled ONLY here: "period" said down a phone line is a length
      // of time, and rewriting it would delete a word the caller meant.
      onPartial: (text) => setDraft(polish(text, { spokenMarks: true }) || text),
    })
    listener.current.start()
    meter.current = new MicLevel()
    void meter.current.start(setLevel)
    setMicOn(true)
  }, [])

  const toggleMic = useCallback(async () => {
    if (micOn) stopMic()
    else if (mode === 'chat') await startDictation()
    else await startMic()
  }, [micOn, mode, startMic, startDictation, stopMic])

  const start = useCallback(async () => {
    if (!agentId) return
    setPhase('connecting')
    setLines([])
    setSummary(null)
    setMemory(null)
    setBooked(null)
    clearSpokenMemory()
    resetEchoWindow()
    sentSoFar.current = ''
    loudSince.current = 0
    bargedIn.current = false
    try {
      const { call_id, greeting } = await api.startCall(agentId, voiceOn ? 'voice' : 'text')
      setCallId(call_id)
      // The details form renders from the agent's intake schema, and the schema arrives with the
      // memory. Fetched here rather than waiting for the first turn to carry it: a caller should
      // be able to fill the form in before saying anything, which is often exactly what they do.
      api.callMemory(call_id).then(setMemory).catch(() => undefined)
      setLines([{ who: 'agent', text: greeting }])
      rememberSpoken(greeting)
      // The gateway synthesises the greeting itself when the neural voice is loaded, so this
      // only runs on the fallback path. Reading the ref rather than the state value is what
      // stops a call opened during startup from speaking it twice, in two different voices.
      if (voiceOn && engine.current === 'browser') {
        speak(greeting, {
          voice: agent?.voice,
          onStart: () => setAgentSpeaking(true),
          onEnd: () => setAgentSpeaking(false),
        })
      }

      socket.current = openCall(call_id, (event) => {
        const type = event.type as string
        if (type === 'token') {
          if (firstTokenAt.current === null) {
            firstTokenAt.current = Math.round(performance.now() - turnStart.current)
            setFirstToken(firstTokenAt.current)
          }
          setLines((c) => {
            const last = c[c.length - 1]
            const spoken = String(event.spoken ?? '')
            if (last?.who === 'agent' && last.streaming) {
              return [...c.slice(0, -1), { ...last, text: spoken }]
            }
            return [...c, { who: 'agent', text: spoken, streaming: true }]
          })
        }
        if (type === 'done') {
          setThinking(false)
          setLines((c) => {
            const last = c[c.length - 1]
            const finished: Line = {
              who: 'agent',
              text: String(event.agent ?? ''),
              timing: event.timing as Timing,
              citations: event.citations as Hit[],
              grounding: event.grounding as Grounding,
              redacted: event.redacted as string[],
              refused: String(event.refused ?? ''),
              tools: event.tools as Line['tools'],
            }
            return last?.who === 'agent' && last.streaming ? [...c.slice(0, -1), finished] : [...c, finished]
          })
          // Only speak here when the gateway is NOT sending audio. With the neural engine the
          // reply arrives as `audio` chunks instead, and doing both would have the agent say
          // every sentence twice in two different voices.
          if (voiceOn && engine.current === 'browser') {
            speak(String(event.spoken ?? event.agent ?? ''), {
              voice: agent?.voice,
              onStart: () => setAgentSpeaking(true),
              onEnd: () => setAgentSpeaking(false),
            })
          }
        }
        if (type === 'audio') {
          const first = event.first_audio_ms as number | null
          if (first != null) {
            setFirstAudioMs(Math.round(first))
            setAgentSpeaking(true)
          }
          // Every chunk re-arms the mute and adds its words to the echo memory. Doing this only
          // on the first chunk left the guard expiring midway through a long reply.
          audio.current?.markSpeaking()
          void audio.current?.push(String(event.wav), String(event.text ?? ''))
        }
        if (type === 'audio_failed') {
          // Fall back to the browser's own voice rather than going silent. Worse, and audible.
          setVoiceEngine('browser')
        }
        // What the agent now believes about the caller, pushed on every turn rather than
        // polled. The panel showing it is the answer to "did it actually hear my phone number",
        // which on a voice call is otherwise unanswerable until the booking is wrong.
        if (event.memory) setMemory(event.memory as unknown as CallMemory)
        if (type === 'booked') setBooked(event as unknown as Booked)
        if (type === 'interrupted') {
          // The gateway has trimmed its record of the reply to what actually played. Shown on
          // screen too, because "why did it stop mid-sentence" is otherwise a mystery.
          setLines((c) => {
            const last = c[c.length - 1]
            if (last?.who !== 'agent') return c
            return [...c.slice(0, -1), { ...last, interrupted: true }]
          })
        }
        if (type === 'summary') { setSummary(event); setPhase('ended') }
        if (type === 'ended') setPhase('ended')
      }, () => setPhase('ended'))

      setPhase('live')

      // A voice call opens with the microphone live -- but NOT until the greeting has finished
      // playing. Starting it immediately meant the recogniser was already running when the
      // greeting came out of the speakers, and it transcribed it: a real call began with
      // "Northgate dental can i help hello my name is..." as the caller's first turn.
      //
      // The echo window cannot help here, because the microphone was listening before there was
      // any audio to detect. The only reliable answer is not to open it yet.
      // Call mode only. In Chat the microphone belongs to whoever presses the button.
      if (mode === 'call' && speechSupported()) {
        greetingGuard.current = window.setTimeout(
          () => { void startMic() },
          // Generous: the greeting is synthesised on demand, so its length is not known here.
          // A second of extra silence at the start of a call costs nothing; a greeting recorded
          // as caller speech derails the whole conversation.
          engine.current === 'kokoro-82m' ? 4200 : 2600,
        )
      }
    } catch (error) {
      // A refusal is not a failure and must not read as one. The commonest reason is capacity,
      // and "all lines are busy" is a thing every caller already understands -- where
      // `Error: 503 "..."` is a thing nobody does.
      const message = String(error)
      const busy = /calls already in progress|capacity/i.test(message)
      setLines([{
        who: 'agent',
        text: busy
          ? 'All lines are busy. This machine carries a fixed number of calls at once so that '
            + 'the ones it does take stay fast — try again in a moment.'
          : `Could not start the call: ${message}`,
      }])
      setPhase('idle')
    }
  }, [agentId, agent, mode, voiceOn, startMic])

  const changeMode = useCallback((next: Mode) => {
    if (next === mode) return
    teardown()
    setMode(next)
    // Back to the beginning, deliberately. The transcript on screen belongs to the mode that
    // produced it, and carrying it over left the button reading "Start again" for something the
    // caller had not started yet.
    setPhase('idle')
    setLines([])
    setSummary(null)
    setMemory(null)
    setBooked(null)
    setDraft('')
    setPartial('')
    setMicOn(false)
    setLevel(0)
  }, [mode, teardown])

  const hangup = useCallback(() => {
    socket.current?.hangup()
    window.clearTimeout(greetingGuard.current)
    stopMic()
    stopSpeaking()
    audio.current?.stop()
    setAgentSpeaking(false)
    setPhase('ended')
  }, [stopMic])


  /* THE SILENCE LOOP.
   *
   * Runs every 60ms while the microphone is on, and is the only thing that ends a turn. It needs
   * FOUR conditions, and each one is there because leaving it out produced a specific failure:
   *
   *   something new to send        the interim transcript repeats everything said since the last
   *                                final result, so without tracking what has already gone, one
   *                                spoken sentence becomes a turn per word
   *   the caller has spoken        otherwise room noise alone ends turns
   *   the microphone is quiet      for as long as this sentence has earned -- the real signal
   *   the transcript has settled   because the microphone is real time and the transcript is
   *                                not. Web Speech delivers words 300-500ms after they were
   *                                said, so audio silence on its own means "they stopped
   *                                talking AND the recogniser may still be writing". Sending
   *                                there truncates the sentence mid-way, which is exactly what
   *                                turned "hi, how are you doing?" into four separate turns.
   */
  useEffect(() => {
    // Chat has no turn-taking to do. The caller presses send; that IS the endpoint.
    if (!micOn || phase !== 'live' || mode !== 'call') return
    const timer = window.setInterval(() => {
      // Polled every 60ms, which is also what keeps the echo window's clock current.
      if (inEchoWindow()) {
        // The TRANSCRIPT is not believed while the agent is audible — the recogniser's stream has
        // no echo cancellation we control. Interrupting still works: that is decided from the
        // level meter, which does. See bargein.ts.
        setTurnReason('agent speaking — talk over it to interrupt')
        return
      }

      const text = lastPartial.current.trim()
      if (!text) return

      // Only the part that has not already been dispatched.
      const now = performance.now()
      const quietFor = now - lastVoiceAt.current
      setSilenceMs(quietFor)

      // A caller who has just said "um" is mid-thought, whatever the silence says. This is the
      // cheapest possible way to avoid interrupting someone, and it is the reason fillers are
      // kept in the transcript the endpointer reads rather than stripped on arrival.
      if (endsOnFiller(text)) {
        setTurnReason('caller trailed off on a filler — still thinking')
        return
      }

      const decision = decideTurn({
        transcript: text,
        alreadySent: sentSoFar.current,
        quietForMs: quietFor,
        settledForMs: now - lastPartialChangeAt.current,
        thresholdMs: lastScore.current.ms,
        agentSpeaking,
        heardSpeech: speechSeen.current,
      })
      setTurnReason(decision.reason)

      if (decision.send) {
        // Last line of defence, at the only point where being wrong actually costs something.
        // Everything above tries to stop echo entering the transcript; this stops it leaving.
        if (looksLikeEcho(decision.text)) {
          sentSoFar.current = text
          setTurnReason('discarded — that was the agent, not the caller')
          return
        }
        speechSeen.current = false
        sentSoFar.current = text
        // Fillers and stutters go no further, and the sentence gets its capital and its
        // question mark. The endpointer needed the raw text; the agent does not, and a turn
        // that was nothing but "um" is not a turn at all.
        const clean = polish(decision.text)
        if (clean) send(clean)
      }
    }, 60)
    return () => window.clearInterval(timer)
  }, [micOn, phase, mode, agentSpeaking, send])

  const live = phase === 'live'

  return (
    <div className="page page-wide">
      <div className="row-between" style={{ marginBottom: 14 }}>
        <div className="row">
          <select
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            disabled={live}
            style={{ width: 210 }}
          >
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name} — {a.business}</option>
            ))}
          </select>

          <div className="seg" role="tablist" aria-label="How to talk to the agent">
            <button role="tab" aria-selected={mode === 'call'} data-on={mode === 'call'}
                    disabled={live} onClick={() => changeMode('call')}
                    title="Hands-free. The agent speaks and decides when you have finished.">
              <Icon name="phone" size={13} /> Call
            </button>
            <button role="tab" aria-selected={mode === 'chat'} data-on={mode === 'chat'}
                    disabled={live} onClick={() => changeMode('chat')}
                    title="Typed. The agent stays silent; the microphone only fills the box.">
              <Icon name="chat" size={13} /> Chat
            </button>
          </div>

          {voiceOn && (
            <span className="chip" data-t={voiceEngine === 'kokoro-82m' ? 'agent' : undefined}
                  title={voiceEngine === 'kokoro-82m'
                    ? 'Kokoro-82M running locally — streamed clause by clause'
                    : 'The browser built-in voice. The neural engine is not loaded.'}>
              {voiceEngine === 'kokoro-82m' ? 'neural voice' : 'browser voice'}
            </span>
          )}

          {live && (
            <span className="chip" data-t="bad">
              <i className="live-dot" /> live
            </span>
          )}
        </div>

        <div className="row">
          {!live && (
            <button className="btn btn-primary" onClick={() => void start()} disabled={!ready || !agentId || phase === 'connecting'}>
              <Icon name={mode === 'call' ? 'phone' : 'chat'} />
              {phase === 'connecting'
                ? 'Connecting…'
                : phase === 'ended'
                  ? (mode === 'call' ? 'Call again' : 'Start again')
                  : (mode === 'call' ? 'Start call' : 'Start chat')}
            </button>
          )}
          {live && (
            <button className="btn btn-danger" onClick={hangup}>
              <Icon name="x" /> {mode === 'call' ? 'Hang up' : 'End chat'}
            </button>
          )}
        </div>
      </div>

      {!ready && (
        <div className="note" data-t="warn" style={{ marginBottom: 14 }}>
          <b>The model is still loading.</b> It takes about twenty seconds on first start — the
          weights are read from disk once and then stay in memory.
        </div>
      )}

      <div className="call">
        <div className="transcript">
          <div className="tr-body" ref={body}>
            {lines.length === 0 && (
              <div className="empty">
                <h3>{mode === 'call' ? 'No call in progress' : 'No chat in progress'}</h3>
                {mode === 'call'
                  ? <>Press <b>Start call</b> and just talk. The agent replies out loud and works
                      out when you have finished on its own — there is nothing to press.</>
                  : <>Press <b>Start chat</b> and type. The agent stays silent. The microphone
                      dictates into the box; you decide when to send it.</>}
              </div>
            )}

            {lines.map((line, i) => (
              <Bubble key={i} line={line} />
            ))}

            {partial && (
              <div className="bubble" data-who="caller">
                <div className="av" data-who="caller">You</div>
                <div>
                  <div className="msg" style={{ opacity: 0.6 }}>
                    {/* Not punctuated yet: a full stop that appears and then jumps along as
                        more words arrive is more distracting than none at all. */}
                    {polish(partial, { punctuate: false }) || partial}
                    <i className="caret" />
                  </div>
                  {endpointMs !== null && (
                    <div className="msg-meta">
                      <span>waiting up to {endpointMs}ms</span>
                      {turnReason && (
                        <span style={{ color: silenceMs > endpointMs * 0.6 ? 'var(--cost)' : 'var(--text-3)' }}>
                          {turnReason}
                        </span>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}

            {thinking && !lines[lines.length - 1]?.streaming && (
              <div className="bubble" data-who="agent">
                <div className="av" data-who="agent">AI</div>
                <div className="msg" style={{ color: 'var(--text-dim)' }}>thinking<i className="caret" /></div>
              </div>
            )}
          </div>

          <div className="tr-foot">
            <div className="composer">
              {speechSupported() && (
                <div className="mic-wrap">
                  <button
                    className="mic" data-on={micOn} data-dictate={mode === 'chat'}
                    onClick={() => void toggleMic()} disabled={!live}
                    title={mode === 'chat'
                      ? (micOn ? 'Stop dictating' : 'Dictate into the box — nothing is sent until you press send')
                      : (micOn ? 'Stop listening' : 'Talk to the agent')}
                  >
                    <Icon name={micOn ? 'mic' : 'mic-off'} size={17} />
                  </button>
                </div>
              )}

              {mode === 'chat' && micOn && (
                <span className="chip" style={{ flexShrink: 0 }}>
                  <Icon name="mic" size={11} /> dictating — press send when you are ready
                </span>
              )}

              {micOn && interrupted && (
                <span className="chip" data-t="cost" style={{ flexShrink: 0 }}>
                  <Icon name="mic" size={12} /> you interrupted
                </span>
              )}

              {micOn && agentSpeaking && !interrupted && (
                <span className="chip" data-t="agent" style={{ flexShrink: 0 }}>
                  <Icon name="volume" size={12} /> agent speaking — talk over it to interrupt
                </span>
              )}

              {micOn && !agentSpeaking && mode === 'call' && (
                <div className="level" aria-hidden>
                  {Array.from({ length: 12 }, (_, i) => (
                    <i key={i} style={{ height: `${Math.max(3, Math.min(22, level * 26 * (1 - Math.abs(i - 5.5) / 9)))}px` }} />
                  ))}
                </div>
              )}

              <textarea
                value={draft}
                placeholder={
                  !live
                    ? (mode === 'call' ? 'Start a call first' : 'Start a chat first')
                    : mode === 'chat'
                      ? 'Type your message, or use the microphone to dictate it…'
                      : 'Or type instead of speaking…'
                }
                disabled={!live}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    send(draft)
                    setDraft('')
                  }
                }}
              />
              <button className="btn btn-primary" disabled={!live || !draft.trim()} onClick={() => { send(draft); setDraft('') }}>
                <Icon name="send" />
              </button>
            </div>
          </div>
        </div>

        <SidePanel
          lines={lines} summary={summary} firstToken={firstToken}
          firstAudioMs={firstAudioMs} voiceOn={voiceOn} voiceEngine={voiceEngine}
          mode={mode} live={live} callId={callId} agentId={agentId}
          memory={memory} booked={booked}
          onMemory={setMemory} onBooked={setBooked}
        />
      </div>
    </div>
  )
}

function Bubble({ line }: { line: Line }) {
  const t = line.timing
  return (
    <div className="bubble" data-who={line.who}>
      <div className="av" data-who={line.who}>{line.who === 'agent' ? 'AI' : 'You'}</div>
      <div style={{ minWidth: 0 }}>
        <div className="msg">
          {line.text}
          {line.streaming && <i className="caret" />}
        </div>

        {line.interrupted && (
          <div className="msg-meta"><span style={{ color: 'var(--cost)' }}>
            you interrupted — the agent only knows the part you heard
          </span></div>
        )}

        {line.redacted && line.redacted.length > 0 && (
          <div className="msg-meta"><span style={{ color: 'var(--bad)' }}>removed before storage: {line.redacted.join(', ')}</span></div>
        )}

        {line.grounding && !line.grounding.ok && (
          <div className="note" data-t="warn" style={{ marginTop: 7, fontSize: 11.5 }}>
            {line.grounding.findings.map((f, i) => (
              <div key={i}><b>{f.value}</b> is not in any document the agent was given</div>
            ))}
            {line.grounding.hedged && <div>estimated a price the documents state exactly</div>}
          </div>
        )}

        {line.refused && (
          <div className="note" data-t="bad" style={{ marginTop: 7, fontSize: 11.5 }}>
            refused a transition the flow does not allow
          </div>
        )}

        {(t || line.citations?.length) && (
          <div className="msg-meta">
            {t && <span>{Math.round(t.total_ms)}ms</span>}
            {t?.think != null && <span>think {Math.round(t.think)}ms</span>}
            {line.citations?.map((c, i) => (
              <span key={i} style={{ color: 'var(--info)' }}>{c.document}</span>
            ))}
            {line.tools?.map((tool, i) => (
              <span key={i} style={{ color: 'var(--agent)' }}>{tool.name} {Math.round(tool.ms)}ms</span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

const STAGES: { key: keyof Timing; label: string; colour: string }[] = [
  { key: 'redact', label: 'redact', colour: '#ff6b81' },
  { key: 'knowledge', label: 'knowledge', colour: '#60a5fa' },
  { key: 'think', label: 'think', colour: '#a78bfa' },
  { key: 'speak', label: 'generate', colour: '#35e0d0' },
  { key: 'tools', label: 'tools', colour: '#ffb340' },
]

function SidePanel({
  lines, summary, firstToken, firstAudioMs, voiceOn, voiceEngine,
  mode, live, callId, agentId, memory, booked, onMemory, onBooked,
}: {
  lines: Line[]
  summary: Record<string, unknown> | null
  firstToken: number | null
  firstAudioMs: number | null
  voiceOn: boolean
  voiceEngine: 'kokoro-82m' | 'browser'
  mode: Mode
  live: boolean
  callId: string
  agentId: string
  memory: CallMemory | null
  booked: Booked | null
  onMemory: (m: CallMemory) => void
  onBooked: (b: Booked) => void
}) {
  const timed = lines.filter((l) => l.timing)
  const last = timed[timed.length - 1]?.timing
  const totals = timed.map((l) => l.timing!.total_ms).sort((a, b) => a - b)
  const median = totals.length ? totals[Math.floor(totals.length / 2)] : 0
  const flagged = lines.filter((l) => l.grounding && !l.grounding.ok).length
  const sources = new Set(lines.flatMap((l) => l.citations?.map((c) => c.document) ?? []))

  return (
    <div className="side-rail">
      {booked && <BookingCard booked={booked} />}

      <DetailsForm
        live={live} callId={callId} memory={memory}
        onMemory={onMemory} onBooked={onBooked}
      />

      {live && <WhatItKnows memory={memory} mode={mode} />}

      <Availability agentId={agentId} bookedRef={booked?.reference ?? ''} />

      <div className="panel">
        <div className="panel-h"><Icon name="clock" size={13} /> Last turn</div>
        <div className="panel-b">
          {!last && <div style={{ color: 'var(--text-dim)', fontSize: 12.5 }}>Nothing measured yet.</div>}
          {last && (
            <>
              <div className="lat">
                {STAGES.map((s) => {
                  const value = Number(last[s.key] ?? 0)
                  const width = last.total_ms ? (value / last.total_ms) * 100 : 0
                  return width > 0.5 ? <i key={s.key} style={{ width: `${width}%`, background: s.colour }} /> : null
                })}
              </div>
              <div className="lat-key">
                {STAGES.filter((s) => Number(last[s.key] ?? 0) > 0.5).map((s) => (
                  <span key={s.key}><i className="sw" style={{ background: s.colour }} />{s.label} {Math.round(Number(last[s.key]))}ms</span>
                ))}
              </div>
              <div style={{ marginTop: 12 }}>
                <div className="stat-line"><span>Total</span><span>{Math.round(last.total_ms)}ms</span></div>
                {firstAudioMs !== null && (
                  <div className="stat-line" title="Silence before the caller heard anything at all">
                    <span>To first audio</span><span>{firstAudioMs}ms</span>
                  </div>
                )}
                {firstToken !== null && (
                  <div className="stat-line" title="Measured in the browser, so it includes the network hop the server-side total does not">
                    <span>To first word (round trip)</span><span>{firstToken}ms</span>
                  </div>
                )}
                <div className="stat-line"><span>Median this call</span><span>{Math.round(median)}ms</span></div>
              </div>
            </>
          )}
        </div>
      </div>

      <div className="panel">
        <div className="panel-h"><Icon name="shield" size={13} /> Checks</div>
        <div className="panel-b">
          <div className="stat-line">
            <span>Unverified numbers</span>
            <span style={{ color: flagged ? 'var(--cost)' : 'var(--good)' }}>{flagged}</span>
          </div>
          <div className="stat-line"><span>Turns</span><span>{timed.length}</span></div>
          <div className="stat-line"><span>Documents used</span><span>{sources.size}</span></div>
          {flagged > 0 && (
            <div className="note" data-t="warn" style={{ marginTop: 10, fontSize: 11.5 }}>
              Every number the agent says is checked against the documents it was given. Flagged
              ones appear in no document.
            </div>
          )}
        </div>
      </div>

      {sources.size > 0 && (
        <div className="panel" style={{ minHeight: 0 }}>
          <div className="panel-h"><Icon name="book" size={13} /> Answered from</div>
          <div className="panel-b">
            {[...sources].map((s) => (
              <div key={s} style={{ fontSize: 12.5, padding: '3px 0', color: 'var(--text-2)' }}>{s}</div>
            ))}
          </div>
        </div>
      )}

      {summary && (
        <div className="panel">
          <div className="panel-h"><Icon name="check" size={13} /> Call ended</div>
          <div className="panel-b">
            <div className="stat-line"><span>Turns</span><span>{String(summary.turns)}</span></div>
            <div className="stat-line"><span>Duration</span><span>{((Number(summary.duration_ms) || 0) / 1000).toFixed(1)}s</span></div>
            <div className="stat-line"><span>Median turn</span><span>{Math.round(Number(summary.median_turn_ms))}ms</span></div>
            <div className="stat-line"><span>Sentiment</span><span>{String(summary.sentiment)}</span></div>
          </div>
        </div>
      )}

      {voiceOn && voiceEngine === 'browser' && !synthSupported() && (
        <div className="note" data-t="warn">
          This browser cannot speak, and the neural voice is not loaded.
        </div>
      )}
      {voiceOn && voiceEngine === 'browser' && synthSupported() && (
        <div className="note" data-t="warn">
          <b>Using the browser's built-in voice.</b> It is robotic. The neural voice needs the
          Kokoro model in <code>services/gateway/models</code>.
        </div>
      )}
      {!speechSupported() && (
        <div className="note" data-t="warn">
          <b>Microphone input needs Chrome or Edge.</b> Typing works everywhere.
        </div>
      )}
    </div>
  )
}


/* ── the details form ────────────────────────────────────────────────────────
 *
 * WHY THE CALLER TYPES THESE. Speech recognition is good at sentences and bad at strings, and a
 * name, a phone number and an email address are strings. One real call produced "tasty mulasson"
 * for a surname and "abc iphone com" for an email address — both plausible English, both wrong,
 * and neither detectable from the transcript.
 *
 * So the agent never asks for them out loud. It fills in what it thinks it heard, marks that as
 * heard rather than known, and a typed value replaces it permanently. Nothing is ever booked on
 * a value the caller has not seen written down.
 */
/* ── what the caller is asked for ────────────────────────────────────────────
 *
 * RENDERED FROM THE AGENT'S OWN SCHEMA, not from three hardcoded inputs. A clinic needs a date
 * of birth, a garage needs a registration, a restaurant needs a party size — and none of them
 * could say so while this component knew the field names in advance.
 *
 * WHY THE CALLER TYPES THESE AT ALL. Speech recognition is good at sentences and bad at strings,
 * and a name, a phone number and an email address are strings. One real call produced "tasty
 * mulasson" for a surname and "abc iphone com" for an email address — both plausible English,
 * both wrong, and neither detectable from the transcript.
 *
 * So the agent never asks for them out loud. It fills in what it thinks it heard, marks that as
 * heard rather than known, and a typed value replaces it permanently. Nothing is ever booked on
 * a value the caller has not seen written down.
 */
function DetailsForm({
  live, callId, memory, onMemory, onBooked,
}: {
  live: boolean
  callId: string
  memory: CallMemory | null
  onMemory: (m: CallMemory) => void
  onBooked: (b: Booked) => void
}) {
  const [form, setForm] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [problems, setProblems] = useState<Record<string, string>>({})
  const [warnings, setWarnings] = useState<Record<string, string>>({})
  const [error, setError] = useState('')

  const fields = memory?.fields ?? []

  // Prefill from whatever the agent picked up, but ONLY into a field the caller has not touched.
  // Overwriting a typed value with a heard one would undo the correction they just made, which
  // is the exact opposite of what this form is for.
  useEffect(() => {
    if (!memory) return
    setForm((current) => {
      const next = { ...current }
      for (const field of memory.fields) {
        const heard = memory.facts[field.key]?.value ?? ''
        if (heard && !current[field.key]) next[field.key] = heard
      }
      return next
    })
  }, [memory])

  const save = useCallback(async () => {
    if (!callId) return
    setSaving(true)
    setError('')
    try {
      const filled = Object.fromEntries(
        Object.entries(form).filter(([, v]) => v.trim()),
      )
      const result = await api.setDetails(callId, filled)
      onMemory(result.memory)
      setProblems(result.problems ?? {})
      setWarnings(result.warnings ?? {})
      // Typing the last missing detail can be the thing that completes the booking, so the
      // server books on the way through and says so. Making the caller repeat a "yes" they have
      // already given is how software starts to feel like paperwork.
      if (result.booked) onBooked(result.booked)
      if (!Object.keys(result.problems ?? {}).length) {
        setSaved(true)
        window.setTimeout(() => setSaved(false), 2200)
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }, [callId, form, onMemory, onBooked])

  const dirty = fields.some(
    (f) => (form[f.key] ?? '').trim() && (form[f.key] ?? '').trim() !== memory?.facts[f.key]?.value,
  )
  // Required and absent, or present but only ever HEARD -- both stop a booking, and a caller
  // cannot tell them apart, so neither does this.
  const blocking = fields.filter(
    (f) => f.required && (
      !memory?.facts[f.key]?.value || memory.facts[f.key]?.confirmed === false
    ),
  )
  const heardNotTyped = (key: string) =>
    Boolean(memory?.facts[key]?.value) && memory?.facts[key]?.confirmed === false

  if (!fields.length) return null

  return (
    <div className="panel">
      <div className="panel-h"><Icon name="user" size={13} /> Your details</div>
      <div className="panel-b">
        <p className="panel-note">
          Typed, not spoken. Names and numbers are what speech recognition gets wrong, so these
          are the values the booking actually uses.
        </p>

        {/* WHAT IS BLOCKING THE BOOKING, said where the caller is looking. A real call went
            eleven turns, settled on a time, and booked nothing -- the system knew the email was
            missing the whole way through and never put that anywhere the caller would see it. */}
        {blocking.length > 0 && memory?.proposed_slot && (
          <div className="note" data-t="warn" style={{ marginBottom: 13 }}>
            <b>Needed before this can be booked:</b>{' '}
            {blocking.map((f) => f.label.toLowerCase()).join(', ')}.
          </div>
        )}

        {fields.map((field) => {
          const problem = problems[field.key]
          const warning = warnings[field.key]
          const value = form[field.key] ?? ''
          return (
            <label key={field.key} className="field">
              <span className="field-l">
                {field.label}
                {blocking.includes(field) && memory?.proposed_slot && (
                  <em className="field-need">needed</em>
                )}
                {!field.required && <em className="field-opt">optional</em>}
                {memory?.facts[field.key]?.confirmed && !problem && (
                  <i className="tick"><Icon name="check" size={10} /></i>
                )}
                {heardNotTyped(field.key) && <em className="field-hint">heard — please check</em>}
              </span>

              {field.kind === 'choice' ? (
                <select
                  value={value}
                  disabled={!live}
                  onChange={(e) => setForm((f) => ({ ...f, [field.key]: e.target.value }))}
                >
                  <option value="">Choose…</option>
                  {field.options.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              ) : field.kind === 'longtext' ? (
                <textarea
                  value={value}
                  disabled={!live}
                  rows={3}
                  data-bad={problem ? true : undefined}
                  onChange={(e) => setForm((f) => ({ ...f, [field.key]: e.target.value }))}
                />
              ) : (
                <input
                  value={value}
                  disabled={!live}
                  data-heard={heardNotTyped(field.key) || undefined}
                  data-bad={problem ? true : undefined}
                  placeholder={placeholderFor(field)}
                  inputMode={inputModeFor(field)}
                  type={field.kind === 'email' ? 'email' : field.kind === 'date' ? 'date' : 'text'}
                  onChange={(e) => setForm((f) => ({ ...f, [field.key]: e.target.value }))}
                  onKeyDown={(e) => { if (e.key === 'Enter') void save() }}
                />
              )}

              {/* A problem outranks the help text: the caller needs to know what to change,
                  not what the field is for. */}
              {problem
                ? <em className="field-bad">{problem}</em>
                : warning
                  ? <em className="field-warn">{warning}</em>
                  : field.help && <em className="field-help">{field.help}</em>}
            </label>
          )
        })}

        <button
          className="btn btn-primary btn-wide"
          disabled={!live || saving || !dirty}
          onClick={() => void save()}
        >
          {saving ? 'Saving…' : saved ? 'Saved' : 'Save details'}
        </button>
        {error && <div className="note" data-t="bad" style={{ marginTop: 8, fontSize: 11.5 }}>{error}</div>}
      </div>
    </div>
  )
}

function placeholderFor(field: IntakeField): string {
  switch (field.kind) {
    case 'phone': return '(212) 555-0142'
    case 'email': return 'you@example.com'
    case 'name': return 'Sam Hassan'
    case 'age': return '34'
    case 'date': return 'YYYY-MM-DD'
    case 'number': return field.minimum != null ? String(field.minimum) : '0'
    default: return ''
  }
}

function inputModeFor(field: IntakeField): 'tel' | 'numeric' | 'email' | undefined {
  switch (field.kind) {
    case 'phone': return 'tel'
    case 'email': return 'email'
    case 'age': case 'number': return 'numeric'
    default: return undefined
  }
}


/* What the agent is holding onto.
 *
 * The answer to "did it actually get my phone number", which on a voice call is otherwise
 * unanswerable until the booking turns out to be wrong. Showing the SOURCE of each value, not
 * just the value, is the point: heard and typed are different kinds of true.
 */
function WhatItKnows({ memory, mode }: { memory: CallMemory | null; mode: Mode }) {
  if (!memory) return null
  const facts = Object.entries(memory.facts).filter(([, f]) => f.value)
  const when = memory.when
  const hasWhen = Boolean(when.day || when.hour !== null || when.part)
  if (!facts.length && !hasWhen && !memory.proposed_slot) return null

  const timing = [
    when.day
      ? new Date(`${when.day}T00:00`).toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })
      : '',
    when.hour !== null
      ? `${String(when.hour).padStart(2, '0')}:${String(when.minute).padStart(2, '0')}`
      : when.part ?? '',
  ].filter(Boolean).join(' · ')

  return (
    <div className="panel">
      <div className="panel-h"><Icon name="book" size={13} /> What the agent knows</div>
      <div className="panel-b">
        {facts.map(([key, fact]) => (
          <div className="stat-line" key={key}>
            <span style={{ textTransform: 'capitalize' }}>{key}</span>
            <span className="know" data-confirmed={fact.confirmed}>
              {fact.value}
              <i>{fact.confirmed ? 'typed' : 'heard'}</i>
            </span>
          </div>
        ))}
        {timing && <div className="stat-line"><span>Wants</span><span>{timing}</span></div>}
        {memory.proposed_slot && (
          <div className="stat-line">
            <span>On the table</span>
            <span style={{ color: memory.slot_confirmed ? 'var(--good)' : 'var(--text-2)' }}>
              {memory.proposed_slot}{memory.slot_confirmed ? ' · agreed' : ''}
            </span>
          </div>
        )}
        {memory.missing.length > 0 && !memory.booked_reference && (
          <p className="panel-note" style={{ marginTop: 9, marginBottom: 0 }}>
            Still needed: {memory.missing.join(', ')}.
            {memory.missing.some((m) => m !== 'reason') && ' Fill those in above rather than saying them.'}
          </p>
        )}
        {memory.unconfirmed.length > 0 && (
          <p className="panel-note" style={{ marginTop: 9, marginBottom: 0, color: 'var(--cost)' }}>
            {memory.unconfirmed.join(' and ')} {memory.unconfirmed.length > 1 ? 'were' : 'was'} heard,
            not typed. Nothing is booked on a value you have not seen written down.
          </p>
        )}
        {mode === 'call' && !facts.length && (
          <p className="panel-note" style={{ marginBottom: 0 }}>Nothing picked up yet.</p>
        )}
      </div>
    </div>
  )
}


function BookingCard({ booked }: { booked: Booked }) {
  const at = new Date(booked.starts_at)
  return (
    <div className="booked-card">
      <div className="booked-h"><Icon name="check" size={14} /> Appointment booked</div>
      <div className="booked-when">{booked.spoken}</div>
      <div className="booked-meta">
        {at.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })}
        {' · '}
        {at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
      </div>
      <div className="booked-row"><span>Reference</span><b>{booked.reference}</b></div>
      {booked.name && <div className="booked-row"><span>Name</span><b>{booked.name}</b></div>}
      {booked.reason && <div className="booked-row"><span>For</span><b>{booked.reason}</b></div>}
    </div>
  )
}


/* The same open slots the agent is reading from, on screen at the same time.
 *
 * Served by an endpoint that calls the same function the conversation calls, so the two cannot
 * drift. A caller told one thing and shown another stops believing both. */
function Availability({ agentId, bookedRef }: { agentId: string; bookedRef: string }) {
  const [slots, setSlots] = useState<Slot[]>([])
  const [total, setTotal] = useState(0)

  useEffect(() => {
    if (!agentId) return
    let alive = true
    api.availability(agentId)
      .then((r) => { if (alive) { setSlots(r.open.slice(0, 6)); setTotal(r.total_open) } })
      .catch(() => undefined)
    return () => { alive = false }
    // bookedRef is a dependency so the list reflects the booking that just happened.
  }, [agentId, bookedRef])

  if (!slots.length) return null
  return (
    <div className="panel">
      <div className="panel-h"><Icon name="clock" size={13} /> Next available</div>
      <div className="panel-b">
        <div className="slots">
          {slots.map((s) => (
            <span className="slot" key={s.iso} title={s.spoken}>
              {new Date(`${s.date}T${s.time}`).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' })}
              <b>{s.time}</b>
            </span>
          ))}
        </div>
        <p className="panel-note" style={{ marginTop: 9, marginBottom: 0 }}>
          {total} open in the next fortnight — the same list the agent is reading from.
        </p>
      </div>
    </div>
  )
}
