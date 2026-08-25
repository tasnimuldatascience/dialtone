# What broke

Every bug in this table was real, is fixed, and has a test that fails without the fix. Together
they are why the gateway suite is 521 tests rather than 300: a suite grows either because somebody
sat down to raise a coverage figure, or because something broke in front of a caller. This is the
second kind.

They are worth reading as a group because most are not logic errors. They are two clocks
disagreeing, a database guarantee that held perfectly while nobody told the caller, an edge on a
diagram that no code path could take. **Almost none would have been found by reading the code.**
They were found by looking at output — a transcript, a screenshot, a measured latency, a call list
with the same value in every row.

| What broke | Why it mattered, and what fixed it |
|---|---|
| The simulator ran callers at 3000 words per minute | Every speed measurement taken against it was meaningless |
| Audio length was counted as it played, not up front | Interruption handling silently did nothing at all |
| Replaying a test call changed the test call | Running it twice tested less the second time |
| One "mm-hmm" got counted 63 times | The check runs 50 times a second and nothing reset it |
| The agent transcribed its own voice and replied to it | Open speakers. Flags went stale between audio chunks, so the guard now asks the audio clock |
| One spoken sentence became four replies | Two clocks disagreed and nothing tracked what had already been sent |
| `sam@example.com` set the appointment reason to "check-up" | "example" contains "exam", and the match was on substrings |
| The caller was told a free morning was full | The prompt said so whenever they had not yet named an hour — which is most of the time |
| **The agent said an appointment was booked when it was not** | The worst thing it can do: the caller hangs up satisfied and finds out on the day. Claims are checked against the database now |
| The agent answered its own sentences | The memory of having spoken expired while the audio was still playing — timed from when it was queued, not when it was heard |
| The whole weight system never rendered | `Inter` was declared and never loaded, so ten weights fell back to two |
| It never asked for the missing details | It was told not to ask for an email out loud, and never told to ask for anything else instead |
| "One more thing" was parsed as one o'clock | It silently moved an appointment already agreed for nine thirty |
| **"I want to speak to a human" was answered by asking what they needed booked** | The `handoff` node existed and three nodes had an edge to it. `_advance` always took the first edge, so the transfer was unreachable |
| "Are you open on Saturday?" was filed as wanting a Saturday appointment | The plural was fixed with a word boundary. That was the wrong diagnosis — the question is about the practice, not about a date |
| **A caller lost a race for a slot and was never told** | The `UNIQUE` constraint worked perfectly. They had agreed a time, the appointment did not exist, and nothing in the conversation said so |
| "Asked about prices — cleaning" for someone booking a cleaning | The history line led with what was looked up, which was the detour, not the journey |
| "Either 8:30 or 9:00?" — "yes, that works" | Offering two times invites an answer that cannot be booked on. It names one now |
| The agent read "[insert location]" out loud | Nothing in the knowledge base gave the practice an address, and a model fills in a form |
| The streaming voice repeated the last few words | It tracked a position in the written reply and used it to slice the spoken one |
| With the gateway down, the dashboard loaded forever | A loading state that never resolves is the least honest thing a UI can do |

## The pattern

Four of these have the same shape, and it is the one worth taking away: **the mechanism worked and
the person was not told.**

`appointments.starts_at` is `UNIQUE`, so two callers cannot hold one slot — and the caller who
lost the race heard nothing about it. The flow graph declared a `handoff` edge — and no code path
could take it. The booking guard refused to book without a confirmed email — and the agent never
asked for one. The false-booking check caught claims the database contradicted — after the caller
had already heard them.

A guarantee nobody communicates is indistinguishable from a bug, and it is harder to find, because
every component passes its own test.
