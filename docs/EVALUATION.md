# How the numbers were measured

This page exists so you can argue with the results in the README. It includes the parts that make
the claim weaker.

---

## The two things being measured

**Speed.** How long the agent stays quiet after the caller genuinely finishes, before it replies.
Lower is better.

**Interruptions.** How often the agent starts talking while the caller is still mid-sentence.
Lower is better. Most vendors never publish this number.

These two fight each other. Both are controlled by one setting — how much silence to wait for.
Turn that setting down and you get a faster agent that interrupts people more. Turn it up and you
get a polite agent that feels slow.

**So a system is only genuinely better if it improves both, or improves one without making the
other worse.** A single "we reply in 600ms" figure tells you nothing, because you cannot see what
it cost.

### Two more numbers we track

**Did it reply at all?** An agent that never speaks has a perfect interruption score. This number
stops that trick working. In the results, `fixed 1200ms` scores 0% interruptions and 0% replies —
the cheat, made visible.

**The slow tail (p90).** A good average with a bad tail means most replies feel quick and some
feel broken. People remember the broken ones.

---

## The test set

60 phone turns, written by hand. 24 where the caller had finished, 36 where they had not.

Each item is a **pause in the middle of someone talking**, labelled with whether they were
actually done, how long the pause lasted, and a rough shape of their tone of voice.

| Type | How many | Why it is included |
|---|---:|---|
| Short answers | 6 | "yes", "sure" — waiting 700ms here is what makes agents feel slow |
| Ordinary finished sentences | 14 | The normal case |
| Questions ending in a verb | 4 | "what appointments do you have" — finished, but ends on a word that usually means unfinished |
| Numbers and codes read aloud | 7 | The most damaging case to get wrong |
| Sentences ending in linking words | 9 | "can I speak to", "my name is" |
| Filler words | 5 | "um", "so basically" — they are still thinking |
| Thinking pauses | 9 | "I already tried restarting it and" |
| Question words that are still unfinished | 3 | "what I need is" — the near-misses that break the rule if written loosely |
| Verbs missing their object | 3 | "can I also get" |

The whole set is published. Run `dialtone bench corpus`, or open the Corpus tab in the web app.
A benchmark with a secret test set is marketing, not evidence.

### How each item is tested

Frame by frame, every 20 milliseconds — not checked once at the end of the pause.

This matters. If a setting *would have* replied after 300ms, we need to catch it doing that, even
when the real pause lasted 700ms. Only checking at the end would flatter every setting, because
each one would get credit for a decision it never actually waited to make.

---

## Where this is weak

**60 items is not many.** Enough to show the method works and to catch things breaking later. Not
enough to settle an argument between vendors. We publish no confidence interval, because one
calculated from 36 negative examples would suggest more precision than 36 examples can support.

**The sentences are written, not recorded.** Real speech recognition makes mistakes, and it makes
them on exactly these cases — numbers and names are what it gets wrong most. The grammar check
reads the transcript, so recognition errors would hurt it in a way this test set cannot show.
Expect the real-world gap to be **smaller** than the numbers here.

**The tone-of-voice data is fake.** Three hand-made shapes (falling, rising, flat) stand in for
real voice pitch. So the tone check is being tested under generous conditions — and it is still
the weaker of the two checks. With real audio its contribution is probably smaller than the
30.6% → 0% figure suggests.

**The scorer now agrees with every single label. That is a warning, not a win.** On a 60-item set
written by the same person who wrote the rules, perfect agreement is exactly what you would
expect from going back and forth between the two. It means the test set has stopped being able to
find anything new.

The last three real bugs were all found by the **call simulator** and the worked example, not by
the test set:

- Questions ending in a verb ("what appointments do you have")
- Verbs with no object ("can I also get")
- Short confirmations ("that's correct")

The honest reading: the test set stops things breaking, and something else has to do the finding.

**The same person wrote the test set and the rules.** That is a real conflict of interest. Two
things reduce it and neither removes it: the test set is published so anyone can check whether the
items are fair, and the web app shows the items the scorer gets wrong instead of hiding them. A
test set labelled by someone else would be better.

**English only.** The grammar rules are English word lists. The approach carries over to other
languages; the word lists do not.

---

## Why the ablation table is the important one

An agent that beats a fixed rule only because its default setting happened to be tuned better is
not smarter — it is just tuned. Comparing one setup against one fixed rule proves nothing, since
the fixed rule could have been set differently.

Two things guard against that:

**1. We compare a whole curve to a whole curve.** Fixed rules from 300ms to 1200ms, and adaptive
setups from 380ms to 720ms. Every adaptive setup lands at 0% interruptions. No fixed rule manages
that while still answering more than 38% of calls.

**2. We turn each check off in turn.** The row that matters is *"adjusts timing, but no checks"*.
With both checks off, the adaptive version is **worse than the plain fixed rule** — it interrupts
100% of unfinished sentences.

That row is the control. It is what makes the final row attributable to the checks rather than to
the machinery around them.

---

## Reproducing it yourself

```bash
cd services/gateway
pip install -e ".[dev]"

dialtone bench ablate      # the main results table
dialtone bench sweep       # the full curve
dialtone bench corpus      # every test item and its score
```

Nothing is cached. Every command recalculates from the source code. That is slower on purpose — a
benchmark you can only reproduce by trusting a saved results file is a claim, not a measurement.

CI re-runs the benchmark on every push and fails the build if the headline result gets worse.

---

## What would prove us wrong

Stated in advance, so the claim can actually be tested:

- **A fixed rule reaching 5% or fewer interruptions at 400ms or faster** on this test set would
  show the adaptive machinery is unnecessary.
- **Real speech-recognition transcripts pushing the grammar check above ~10% interruptions**
  would show it does not survive recognition errors.
- **A test set labelled by someone else giving clearly worse results** would show this test set
  was shaped around the rules rather than the other way round.
