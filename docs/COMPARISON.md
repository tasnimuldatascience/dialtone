# Where dialtone sits

An audit of dialtone against the four commercial platforms it resembles — **Retell AI**, **Vapi**,
**Bland AI** and **ElevenLabs Agents** — done in August 2026 from their own documentation.

The point of this file is not to claim dialtone wins. It does not, and on most of the rows below
it cannot. The point is that a repository which imitates a category should be able to say exactly
which parts of that category it implements, which it deliberately does not, and why — and that is
a more useful document than a feature list with every box ticked.

---

## What dialtone is

**A working implementation of the parts of a voice agent that are hard to get right, with the
measurements to show it.** It runs entirely on one machine, on free weights, with no API keys.

It is **not** a hosted platform, and the gap is not incidental — see [What it is not](#what-it-is-not).

---

## The one row where dialtone is ahead

**Turn-taking is measured and published.** Every platform in this table markets latency; none of
them publishes the number that matters alongside it.

| | latency claim | false-cutoff rate published? | test set published? |
|---|---|---|---|
| Retell | "~600ms" (marketing) | no | no |
| Vapi | "<500ms" marketing / "~800ms" in their own FAQ | no | no |
| Bland | "sub-400ms" | no | no |
| ElevenLabs | 75ms TTS / 150ms STT (component-level, precise) | no | no |
| **dialtone** | **280ms median** | **0.0%, against 16.7% for a fixed 700ms rule** | **all 60 turns, in the repo** |

Speed on its own is not a result. An agent gets faster at cutting people off by lowering one
number, and every claim above is compatible with that. The interesting figure is the pair, and
dialtone publishes both plus the corpus they were computed on, so the claim can be checked rather
than believed.

Two caveats, stated because they matter:

- **60 hand-labelled turns is a small test set.** Enough to show a method works; not enough to
  settle an argument between vendors. [EVALUATION.md](EVALUATION.md) lists every limitation.
- **These are not comparable numbers.** dialtone measures endpointing decision latency on a
  labelled corpus. The vendors are quoting production time-to-first-audio. An
  [independent benchmark](https://openbenchmarks.com/voice-agent-latency) measuring
  caller-experienced time found **none of the five platforms it tested had a median below one
  second**, and that platform self-reported latency ran ~490ms below what the caller actually
  experienced. Component latency and perceived latency are different quantities, and every
  number in the table above — dialtone's included — is the first kind.

---

## Rows where dialtone is competitive

Not ahead, but genuinely implemented rather than stubbed.

| capability | dialtone | notes |
|---|---|---|
| **Barge-in** | ✅ | Truncates history to *played* audio, not generated text. Retell, Vapi and Bland all do this; ElevenLabs' is the thinnest. dialtone's runs in a browser with only one echo-cancelled stream, which is harder than it sounds. |
| **Grounding** | ✅ | Every number the agent says is checked against the passages it was actually given. **None of the four does this as a first-class feature.** Retell's AI QA measures a "hallucination rate" post-hoc; dialtone checks per turn, before the caller hears it. |
| **PII redaction** | ✅ streaming | Retell's is far broader (13 categories, audio beep-over, tool arguments). Vapi offers none at all. dialtone's covers card numbers and the common identifiers, in the stream, before storage. |
| **Conversation graph** | ✅ | Node scoping of tools, validated before load, all paths enumerable. Comparable in shape to Retell's and Bland's; far fewer node types. Vapi **retired** its graph builder in August 2026. |
| **Knowledge / RAG** | ✅ | BM25 + dense fusion, absolute relevance gate applied before normalisation, lexical small-talk filter. The measurements behind the gate are in the README, which none of the four publishes. |
| **Simulation testing** | ✅ | Scripted calls, replayable, deterministic. All four have deeper versions — Bland's especially (flakiness detection, auto-fix loops). |
| **Idempotency on money paths** | ✅ | Plus a `UNIQUE` constraint on the appointment slot, so two callers cannot take the same one. |
| **Typed intake with validation** | ✅ | Operator-declared fields, nine kinds, validated before storage. Comparable to the structured-extraction features, though those extract from the transcript rather than from a form. |

---

## What is missing

Ordered by how much it would matter to someone actually deploying this.

### It cannot make or receive a real phone call

`telephony/provider.py` is a **six-method interface plus a simulator**. There is no Twilio, no
SIP, no carrier. Every one of the four ships:

- inbound and outbound over PSTN, SIP trunking with published IP ranges and codec lists
- number provisioning (Retell $2/mo, Bland $15/mo, Vapi free US numbers)
- warm transfer — Retell has three modes including an agentic screener; dialtone has none
- voicemail detection, DTMF send and receive, call recording

This is the single largest gap and it is deliberate: implementing the interface against Twilio is
roughly a day's work, and doing it would have meant a repository nobody can run without an
account and a phone bill.

### No production operations layer

| missing | who has it |
|---|---|
| Post-call webhooks with HMAC signatures | all four |
| Outbound campaigns / batch calling from CSV | all four |
| Concurrency limits, burst, queueing | all four, with published numbers |
| Agent versioning, drafts, rollback | all four (ElevenLabs has branch/merge/**rebase**) |
| A/B testing on live traffic | Retell, ElevenLabs |
| Alerting on metric thresholds | Retell (14 metrics), Vapi, Bland |
| Live listen / human takeover mid-call | Retell, Bland, Vapi |
| SSO, RBAC, audit logs | all four |
| MCP (either direction) | **all four** |

MCP is worth calling out: every one of the four now ships both an outbound MCP client and a
hosted MCP server for managing the account. dialtone has an internal tool registry and no MCP at
all.

### Model and voice breadth

dialtone runs **one** LLM (Qwen2.5-1.5B), **one** TTS (Kokoro-82M), **one** embedding model, and
uses the browser for speech recognition. Vapi offers 18 TTS providers and 12 STT providers. Retell
publishes a 23-model LLM enum. ElevenLabs has the best voice quality in the category and voice
cloning to match.

dialtone has none of that, and the small model shows — see the [What this is not](../README.md)
section of the README. What it buys is that the whole thing runs on a laptop with no account.

### Compliance

SOC 2, HIPAA BAAs, PCI, data residency, retention policies. All four have some or all. dialtone
has redaction and a retention-shaped schema, and no attestations of any kind — which is the
correct state for a repository rather than a company.

---

## What it is not

**It is not a product, and pretending otherwise would be the actual dishonesty.** It is a
single-tenant, single-machine implementation of the interesting half of a voice agent, with the
boring-but-essential half (carriers, tenancy, billing, compliance, scale) deliberately absent.

Anyone choosing between this and Retell is not really choosing. What this repository is for is
showing that the hard parts — knowing when someone has stopped speaking, giving way when they cut
in, refusing to say a number that is not in a document, deciding an irreversible action in code
rather than in a prompt — are understood well enough to build, measure, and argue about.

---

## Sources

Feature claims about the four platforms come from their own documentation, read in August 2026:
[docs.retellai.com](https://docs.retellai.com), [docs.vapi.ai](https://docs.vapi.ai),
[docs.bland.ai](https://docs.bland.ai),
[elevenlabs.io/docs/eleven-agents](https://elevenlabs.io/docs/eleven-agents). The independent
latency measurement is [openbenchmarks.com/voice-agent-latency](https://openbenchmarks.com/voice-agent-latency).

Two things worth knowing if you check these yourself: **Vapi's visual workflow builder was retired
on 18 August 2026** and replaced by "Squads", so any comparison against Vapi Workflows is against
a removed product; and **ElevenLabs rebranded to ElevenAgents**, moving its docs to
`/docs/eleven-agents/`.
