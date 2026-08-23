<div align="center">

# dialtone

**An AI phone agent that knows when you have finished talking.**

[![tests](https://img.shields.io/badge/tests-127%20passing-4ade80?style=flat-square)](#run-it)
[![python](https://img.shields.io/badge/python-3.12%2B-35e0d0?style=flat-square)](#run-it)
[![typescript](https://img.shields.io/badge/typescript-5.6-35e0d0?style=flat-square)](#run-it)
[![license](https://img.shields.io/badge/license-MIT-8ea0b5?style=flat-square)](LICENSE)

</div>

---

## What is this?

Software for building AI agents that answer phone calls — like Retell.ai or Vapi.

The hard part of a phone agent is not the AI. It is knowing **when the caller has stopped
speaking** so the agent can reply. Reply too slowly and the call feels broken. Reply too quickly
and you cut the caller off mid-sentence.

dialtone measures both, and publishes the trade-off.

---

## The problem, in one example

A caller reads out their account number. They pause to think.

```
Caller:  "My account number is four two four two..."
             ↑ pauses for 0.8 seconds

A normal agent:  starts talking. It thinks the caller finished.
dialtone:        stays quiet. The sentence ends on a number, so more is coming.

Caller:  "...four two four two."
```

Most systems wait a **fixed** amount of silence — usually 700 milliseconds — then reply. That
single rule cannot tell these two cases apart:

| The caller said | Finished? | A fixed 700ms rule |
|---|---|---|
| "Yes" | Yes | Waits 700ms. Feels slow. |
| "My account number is four two" | No | Replies. **Cuts them off.** |

dialtone changes how long it waits based on **what the caller actually said**.

---

## The result

Tested on 60 hand-labelled phone turns:

| | Normal fixed 700ms | dialtone |
|---|---:|---:|
| **Speed** — how long before it replies | 700ms | **280ms** |
| **Interruptions** — how often it cuts callers off | 16.7% | **0%** |

**2.5× faster, and it stopped interrupting people.**

Usually these two trade against each other: making an agent faster means cutting people off more
often. Getting both at once is the point of this project.

![The benchmark](docs/img/benchmark.png)

---

## Run it

You need Python 3.12+ and Node 20+. No API keys. No GPU. Nothing paid.

```bash
# 1. The backend
cd services/gateway
pip install -e ".[serve,dev]"

pytest                    # 127 tests
dialtone bench ablate     # see the results table
dialtone serve            # starts on http://127.0.0.1:8071

# 2. The web app (in a second terminal)
cd apps/studio
npm install
npm run dev               # open http://localhost:5173
```

### Try this first

Ask the system how long it would wait for two different sentences:

```console
$ dialtone bench score "my account number is four two"
completion   0.05  (ends mid-number — caller is reading something out)
threshold    1672ms of silence before responding

$ dialtone bench score "what appointments do you have"
completion   0.88  (WH-question with a fronted object, ending on 'have')
threshold    246ms of silence before responding
```

Same system. It waits **6.8× longer** for the person reading a number, because that sentence
clearly is not finished.

---

## How it decides

Three checks run on every 20 milliseconds of audio:

| Check | Question it asks | Example |
|---|---|---|
| **Silence** | How long have they been quiet? | Everyone does this one |
| **Grammar** | Can this sentence end here? | "my name is" → no, keep waiting |
| **Tone** | Is their voice going down? | Voice rising → they are continuing |

The waiting time slides between **160ms** (clearly finished) and **1800ms** (clearly
mid-sentence).

**It uses rules, not a machine-learning model.** Three reasons:

- Fast enough to run on every single audio frame.
- Needs no GPU, and cannot make things up.
- When it cuts someone off, you can read the rule that caused it and fix it.

### Do the checks actually help?

We turned each one off to find out:

| Setup | Speed | Cuts callers off |
|---|---:|---:|
| Normal fixed 700ms | 700ms | 16.7% |
| **Adjusts timing, but no checks** | 520ms | **100%** ← worse than doing nothing |
| Grammar check only | 380ms | 0% |
| Tone check only | 440ms | 30.6% |
| **Both (what ships)** | **280ms** | **0%** |

Look at row two. Adjusting the timing **without** the checks is worse than the plain fixed rule
— it interrupts every single unfinished sentence. That row proves the improvement comes from the
checks, not from luckier settings.

---

## Interruptions: the bug most phone agents have

When a caller talks over the agent, you stop the audio. Simple. But there is a trap.

The agent **wrote** a whole sentence. The caller only **heard** the part that played before they
cut in.

```
Agent wrote:   "I've got Tuesday at nine, Tuesday at ten thirty,
                Wednesday at noon, and Friday at four."
Caller heard:  "I've got Tuesday at…"          ← they cut in here
```

If you save the full sentence into the conversation history, the AI now believes it offered four
appointment times. Two turns later it says *"as I mentioned, Tuesday at ten thirty…"* — and the
caller has no idea what it is talking about.

**dialtone saves what the caller heard, not what the AI wrote.**

![The call monitor](docs/img/monitor.png)

It also tells the difference between a real interruption and someone just saying **"mm-hmm"**.
An agent that stops every time you say "uh huh" can never finish a sentence.

---

## Testing phone calls without a phone

Testing a voice agent normally needs a person, a phone, and 40 seconds per attempt. You cannot
run that in CI, so the tricky cases get tested once by hand and then never again.

dialtone includes a **call simulator**. It replays scripted callers through the real code and
gives the same answer every time.

```console
$ dialtone call run account-number

Caller reads a number aloud  (account-number)
  turns 2   median endpoint 1680ms (vs 700ms fixed)   false cutoffs 0
```

This call is **slower** than normal — 1680ms — and that is correct. The caller paused twice
while reading their account number, and dialtone waited. A fixed rule would have replied over
them both times and captured half a number.

Waiting costs time. We show that cost instead of hiding it, and there is a test asserting this
call is slower than a normal one.

Six scenarios ship:

| Scenario | What it checks |
|---|---|
| `booking` | A normal call, start to finish |
| `account-number` | Caller pauses while reading numbers |
| `barge-in` | Caller talks over the agent |
| `backchannel` | Caller says "mm-hmm" — agent should keep going |
| `card-number` | Caller reads a card number aloud |
| `packet-loss` | A bad phone line dropping 3% of the audio |

---

## Keeping card numbers out of the system

People read card numbers **out loud** on phone calls. Nobody says "4242424242424242" — they say
*"four two, four two, four two…"*.

Most redaction tools look for digits. On a phone call there are no digits, so they find nothing
and report that the call was clean. That is the worst possible failure.

![Redaction](docs/img/compliance.png)

dialtone converts spoken numbers into digits first, then removes them.

**It also keeps what should be kept.** An order number looks just like a card number. dialtone
runs the Luhn check — a maths test that every real card passes and most random numbers fail:

| Caller says | What happens |
|---|---|
| A real card number | Removed, replaced with `[CARD]` |
| An order number | Kept — the agent needs it to help them |

The AI model never receives the card number at all. A model that never sees a card number cannot
repeat one back.

---

## Controlling what the agent can do

You describe the call as a **flow chart**. Each step says what the agent is trying to achieve and
which tools it is allowed to use.

![The flow](docs/img/flow.png)

The AI still chooses its own words. The chart only controls what is **possible**.

```console
$ dialtone flow show
node           kind      collects       tools reachable here                 edges
greet          speak                    —                                    3
identify       collect   name           lookup_patient                       2
offer_slots    tool                     check_availability                   3
book           tool                     book_appointment, send_confirmation  3
handoff        transfer                 —                                    0
goodbye        end                      —                                    0
```

`book_appointment` exists at the `book` step and nowhere else. If the AI decides to book an
appointment during the greeting, it cannot — the tool is not in the list it was given.

![Node detail](docs/img/flow-node.png)

### Slow tools need covering

On a phone call, a slow database lookup is **silence**, and silence sounds like the line dropped.
So every tool declares how slow it is:

| Speed | Time | What happens |
|---|---|---|
| `INSTANT` | under 0.15s | Just run it |
| `FAST` | under 0.8s | Fits inside a natural pause |
| `SLOW` | under 4s | Agent says *"let me check that"* **first** |
| `BACKGROUND` | over 4s | Cannot wait — promise a callback |

### Not booking things twice

If the line drops between "book it" and the confirmation, the caller rings back. Without care,
they get booked twice.

Tools that change something — bookings, payments — get an **idempotency key**. Same key means
same action, so it only happens once. There is a test for this, plus a second test proving it
*does* double-book without the key. That way the first test is really measuring the guard.

---

## The test set

![The corpus](docs/img/corpus.png)

60 phone turns, labelled by hand, **published in full**. A benchmark with a secret test set is
just marketing.

They cover what actually goes wrong on phone lines: numbers read out loud, sentences ending in
"and", filler words like "um", short answers like "yes", and thinking pauses.

60 items is small, and we say so. It is enough to show the method works. It is not enough to
settle an argument between vendors. [docs/EVALUATION.md](docs/EVALUATION.md) lists every
limitation, including the ones that weaken the claim.

---

## All commands

```
dialtone bench ablate         which checks are doing the work
dialtone bench sweep          the full speed vs interruptions curve
dialtone bench score TEXT     why it would or would not reply to this sentence
dialtone bench corpus         the test set and its scores
dialtone call list            available test scenarios
dialtone call run ID -v       replay one call, step by step
dialtone flow show            the flow chart, its rules, and every possible path
dialtone flow validate        find broken flows before a real call does
dialtone redact TEXT          what the AI sees, and what gets removed
dialtone serve                start the API
```

---

## Project layout

```
services/gateway/src/dialtone/
├── turn/
│   ├── endpointing.py    decides when the caller has finished
│   └── bargein.py        handles interruptions and "mm-hmm"
├── pipeline/             the listen → think → speak loop
├── flow/                 the flow chart and its rules
├── tools/                tool speed classes and safety guards
├── telephony/            phone line handling and the call simulator
├── compliance/           removing card numbers and personal data
├── eval/                 the benchmark and the test set
├── sim/                  full simulated calls
├── agents/               a complete worked example agent
└── server/               the API for the web app

apps/studio/src/          the web app (React + TypeScript)
```

### Where the time goes

A caller stops believing it is a conversation after about 700–800ms of silence. You only fit in
that budget if every stage starts on the **first word** of the previous stage, instead of waiting
for it to finish:

| Stage | If you wait | If you stream | Budget |
|---|---:|---:|---:|
| Deciding the caller finished | 700ms | 280ms | 300ms |
| Converting speech to text | 380ms | 55ms | 80ms |
| AI's first word | 640ms | 210ms | 240ms |
| First audio out | 340ms | 65ms | 100ms |
| **Total** | **~2060ms** | **~610ms** | **720ms** |

The "if you wait" column is what you get from writing the obvious code. It is why many voice
agents feel like a walkie-talkie.

---

## Tests

127 tests. Each is named after the problem it prevents, not the function it calls:

```
test_a_dropped_line_does_not_double_charge_the_caller
test_history_records_what_the_caller_heard_not_what_was_generated
test_a_spoken_card_does_not_destroy_the_following_word_boundary
test_holding_through_a_number_costs_latency_and_that_is_the_point
```

Some exist because the code was wrong first:

| The bug | Why it mattered |
|---|---|
| The simulator ran callers at 3000 words per minute | Every speed measurement taken against it was meaningless |
| Audio length was counted as it played, not up front | Interruption handling silently did nothing at all |
| Replaying a test call changed the test call | Running it twice tested less the second time |
| One "mm-hmm" got counted 63 times | The check runs 50 times a second and nothing reset it |

---

## What this is not

It does not include speech recognition, an AI model, or a voice synthesiser. It defines how those
plug in, and measures **the gaps between them** — which is where phone agents actually break.

The phone-line layer is an interface plus a simulator. Connecting a real provider like Twilio
means implementing six functions.

---

## Licence

MIT. See [LICENSE](LICENSE).
