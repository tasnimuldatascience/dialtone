# Evaluation methodology

This document exists so the headline numbers can be argued with. Everything below is what a
sceptical reader should know before believing the README, including the parts that weaken the
claim.

---

## What is being measured

Two quantities, always together, because reporting either alone is meaningless:

**Endpoint latency** — milliseconds of silence after the caller genuinely finishes, before the
system declares the turn over. Lower is better.

**False cutoff rate** — the share of *unfinished* turns where the system wrongly declared the
turn over. This is the metric no vendor publishes, and it is the price of every latency figure.
Lower is better.

They trade against each other through one dial (the silence threshold), so a system is only
better if it improves both, or improves one without worsening the other. Sliding along the
existing curve is not an improvement, and a single published number cannot distinguish the two.

Two secondary quantities:

**Completion recall** — the share of finished turns endpointed at all within the labelled pause.
A system that never responds has a perfect false-cutoff rate, so this stops the metric being
gamed by simply waiting forever. `fixed 1200ms` in the sweep scores 0% false cutoff and 0%
recall, which is the degenerate case made visible.

**p90 latency** — the slow tail. A good median with a bad p90 means most turns feel responsive
and some feel broken, and callers remember the broken ones.

---

## The corpus

60 hand-written turns: 24 complete, 36 incomplete. Each item is a **pause inside a caller turn**,
labelled with whether the caller had actually finished at that moment, plus how long the pause
lasted and a coarse energy contour.

Categories, chosen from the failure cases that actually occur on a phone line:

| category | n | why it is there |
|---|---:|---|
| short confirmations | 6 | *"yes"*, *"sure"* — waiting 700 ms here is most of what makes an agent feel sluggish |
| ordinary complete turns | 14 | the baseline case |
| WH-questions ending on a verb | 4 | *"what appointments do you have"* — complete, but ends on a word that normally signals incompleteness |
| numbers and codes read aloud | 7 | the most damaging false cutoff there is |
| dangling function words | 9 | *"can I speak to"*, *"my name is"* |
| fillers | 5 | *"um"*, *"so basically"* — the caller is composing |
| thinking pauses mid-sentence | 9 | *"I already tried restarting it and"* |
| WH-openers that are genuinely unfinished | 3 | *"what I need is"* — the near-misses that break the WH rule if it is written any looser |
| transitive verbs with no object | 3 | *"can I also get"* — found by the worked example, not the corpus |

The corpus is published in full (`dialtone bench corpus`, and in the studio). A benchmark whose
test set is private is a marketing number.

### How items are replayed

Frame by frame, at 20 ms steps, not evaluated once at the final pause length. This matters: a
configuration that *would have* endpointed at 300 ms must be caught doing so even when the
labelled pause ran to 700 ms. Evaluating only at the end flatters every configuration, because
each one gets credit for a decision it would never have waited to make.

---

## Honest limitations

**60 items is small.** It is enough to demonstrate a methodology and to catch a regression. It is
not enough to settle a vendor comparison, and no confidence interval is reported because one
computed on 36 negatives would imply a precision the sample size does not support.

**The transcripts are written, not transcribed.** A real recogniser produces errors, and its
errors are correlated with exactly the cases here — numbers and proper nouns are what ASR gets
wrong. The syntactic signal reads the transcript, so recogniser errors degrade it in a way this
corpus cannot measure. Expect the real-world gap to be smaller than the one reported here.

**The energy contours are synthetic.** Three hand-built shapes (falling, rising, flat) stand in
for real pitch tracks. The prosodic signal is therefore evaluated under favourable conditions,
and the ablation shows it is the weaker of the two signals even so — real contours are noisier,
so its true contribution is likely smaller than the 30.6% → 0% figure suggests.

**The scorer currently agrees with every label.** That is not a result, it is a warning. On a
60-item set written by the same person who wrote the rules, perfect agreement is the expected
outcome of iterating between the two, and it means the corpus has stopped being able to find
anything. The last three real defects — the WH-fronted-object case, transitive verbs with no
object, and short turns ending on a confirmation word — were all found by the **call simulator**
and the worked example, not by the corpus. That is the honest reading: the corpus guards against
regression, and something else has to do the discovery.

**The author wrote both the corpus and the rules.** That is a genuine conflict. Two things
mitigate it and neither eliminates it: the corpus is published so anyone can check whether the
items are fair, and the studio surfaces the items where the scorer disagrees with the label
rather than hiding them. A held-out set labelled by someone else would be better.

**English only.** The syntactic rules are English word lists. The architecture generalises; the
tables do not.

---

## Why the ablation is the important table

An adaptive endpointer that beats a fixed one only because its base threshold happens to be
better tuned is not adaptive — it is tuned. Comparing one adaptive configuration to one fixed
threshold proves nothing at all, because the fixed one could simply have been set differently.

Two things guard against that:

1. **The sweep compares a curve to a curve.** Fixed thresholds from 300 ms to 1200 ms, adaptive
   base thresholds from 380 ms to 720 ms. Every adaptive configuration sits at 0% false cutoff;
   no fixed threshold achieves that above 38% recall.

2. **The ablation turns each signal off in turn.** The row that matters is `adaptive, no
   signals`: with both signals disabled the adaptive endpointer is *strictly worse than the
   baseline*, interrupting 100% of unfinished turns. That row is the control. It is what makes
   the last row attributable to the signals rather than to the machinery around them.

---

## Reproducing

```bash
cd services/gateway
pip install -e ".[dev]"
dialtone bench ablate      # the headline table
dialtone bench sweep       # the full curve
dialtone bench corpus      # every labelled item, and the score assigned to it
```

Nothing is cached. Every command recomputes from source, which is slower and is the point — a
benchmark reproducible only by trusting a checked-in JSON file is a claim, not a measurement.
CI re-runs the ablation on every push and fails the build if the headline result regresses.

---

## What would change the conclusion

Stated in advance, so the claim is falsifiable:

- A fixed threshold reaching **≤5% false cutoff at ≤400 ms median** on this corpus would show the
  adaptive machinery is unnecessary.
- Real ASR transcripts moving the syntax-only false-cutoff rate above **~10%** would show the
  signal does not survive recogniser noise.
- A held-out corpus labelled by someone else showing materially worse numbers would show the
  corpus is tuned to the rules rather than the other way round.
