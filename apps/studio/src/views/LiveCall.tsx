import { useCallback, useEffect, useRef, useState } from 'react'
import type { ViewProps } from '../App'
import { api, openCall, type Grounding, type Hit, type Timing } from '../api'
import { Icon } from '../components/Icon'
import { AudioQueue, Listener, MicLevel, agentAudible, clearSpokenMemory, loadVoices, rememberSpoken, setAgentAudioProbe, speak, speechSupported, stopSpeaking, synthSupported } from '../voice'
import { decideTurn } from '../turntaking'

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
 */

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
}

type Phase = 'idle' | 'connecting' | 'live' | 'ended'



export function LiveCall({ agent, agents, agentId, setAgentId, ready }: ViewProps) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [lines, setLines] = useState<Line[]>([])
  const [draft, setDraft] = useState('')
  const [thinking, setThinking] = useState(false)
  const [voiceOn, setVoiceOn] = useState(false)
  const [micOn, setMicOn] = useState(false)
  const [level, setLevel] = useState(0)
  const [partial, setPartial] = useState('')
  const [endpointMs, setEndpointMs] = useState<number | null>(null)
  const [firstToken, setFirstToken] = useState<number | null>(null)
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null)
  const [agentSpeaking, setAgentSpeaking] = useState(false)
  const [silenceMs, setSilenceMs] = useState(0)
  const [turnReason, setTurnReason] = useState('')
  const [voiceEngine, setVoiceEngine] = useState<'kokoro-82m' | 'browser'>('browser')
  const [firstAudioMs, setFirstAudioMs] = useState<number | null>(null)

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
        if (agentAudible()) return

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
      // Voice activity. The floor is well above room noise and well below speech; a laptop
      // microphone in a quiet room idles around 0.02.
      //
      // Ignored entirely while the agent is audible: on open speakers the microphone hears the
      // agent loudly, and counting that as caller speech kept resetting the silence timer, so
      // the turn could not end while the agent was talking OR for a while afterwards.
      if (value > 0.08 && !agentAudible()) {
        lastVoiceAt.current = performance.now()
        speechSeen.current = true
      }
    })
    setMicOn(true)
  }, [rescore])

  const toggleMic = useCallback(async () => {
    if (micOn) stopMic()
    else await startMic()
  }, [micOn, startMic, stopMic])

  const start = useCallback(async () => {
    if (!agentId) return
    setPhase('connecting')
    setLines([])
    setSummary(null)
    clearSpokenMemory()
    sentSoFar.current = ''
    try {
      const { call_id, greeting } = await api.startCall(agentId, voiceOn ? 'voice' : 'text')
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
          audio.current?.markSpeaking(String(event.text ?? ''))
          void audio.current?.push(String(event.wav))
        }
        if (type === 'audio_failed') {
          // Fall back to the browser's own voice rather than going silent. Worse, and audible.
          setVoiceEngine('browser')
        }
        if (type === 'summary') { setSummary(event); setPhase('ended') }
        if (type === 'ended') setPhase('ended')
      }, () => setPhase('ended'))

      setPhase('live')

      // A voice call opens with the microphone live. Requiring a second click before the agent
      // can hear anything is not how a telephone behaves, and it is the first thing anyone
      // trying this trips over.
      if (voiceOn && speechSupported()) {
        void startMic()
      }
    } catch (error) {
      setLines([{ who: 'agent', text: `Could not start the call: ${String(error)}` }])
      setPhase('idle')
    }
  }, [agentId, agent, voiceOn, startMic])

  const hangup = useCallback(() => {
    socket.current?.hangup()
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
    if (!micOn || phase !== 'live') return
    const timer = window.setInterval(() => {
      const text = lastPartial.current.trim()
      if (!text) return

      // Only the part that has not already been dispatched.
      const now = performance.now()
      const quietFor = now - lastVoiceAt.current
      setSilenceMs(quietFor)

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
        speechSeen.current = false
        sentSoFar.current = text
        send(decision.text)
      }
    }, 60)
    return () => window.clearInterval(timer)
  }, [micOn, phase, agentSpeaking, send])

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

          <label className="switch">
            <input type="checkbox" checked={voiceOn} onChange={(e) => setVoiceOn(e.target.checked)} disabled={live} />
            Speak replies
          </label>

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
              <Icon name="phone" />
              {phase === 'connecting' ? 'Connecting…' : phase === 'ended' ? 'Call again' : 'Start call'}
            </button>
          )}
          {live && (
            <button className="btn btn-danger" onClick={hangup}>
              <Icon name="x" /> Hang up
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
                <h3>No call in progress</h3>
                Press <b>Start call</b>, then type — or turn on the microphone and talk.
              </div>
            )}

            {lines.map((line, i) => (
              <Bubble key={i} line={line} />
            ))}

            {partial && (
              <div className="bubble" data-who="caller">
                <div className="av" data-who="caller">You</div>
                <div>
                  <div className="msg" style={{ opacity: 0.6 }}>{partial}<i className="caret" /></div>
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
                  <button className="mic" data-on={micOn} onClick={() => void toggleMic()} disabled={!live} title={micOn ? 'Stop listening' : 'Talk to the agent'}>
                    <Icon name={micOn ? 'mic' : 'mic-off'} size={17} />
                  </button>
                </div>
              )}

              {micOn && agentSpeaking && (
                <span className="chip" data-t="agent" style={{ flexShrink: 0 }}>
                  <Icon name="volume" size={11} /> agent speaking — mic paused
                </span>
              )}

              {micOn && !agentSpeaking && (
                <div className="level" aria-hidden>
                  {Array.from({ length: 12 }, (_, i) => (
                    <i key={i} style={{ height: `${Math.max(3, Math.min(22, level * 26 * (1 - Math.abs(i - 5.5) / 9)))}px` }} />
                  ))}
                </div>
              )}

              <textarea
                value={draft}
                placeholder={live ? 'Type what the caller says…' : 'Start a call first'}
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
}: {
  lines: Line[]
  summary: Record<string, unknown> | null
  firstToken: number | null
  firstAudioMs: number | null
  voiceOn: boolean
  voiceEngine: 'kokoro-82m' | 'browser'
}) {
  const timed = lines.filter((l) => l.timing)
  const last = timed[timed.length - 1]?.timing
  const totals = timed.map((l) => l.timing!.total_ms).sort((a, b) => a - b)
  const median = totals.length ? totals[Math.floor(totals.length / 2)] : 0
  const flagged = lines.filter((l) => l.grounding && !l.grounding.ok).length
  const sources = new Set(lines.flatMap((l) => l.citations?.map((c) => c.document) ?? []))

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minHeight: 0 }}>
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
