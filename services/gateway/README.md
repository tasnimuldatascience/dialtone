# dialtone gateway

The Python service: turn-taking, flows, tools, telephony, compliance, and the benchmark.

See the [repository README](../../README.md) for what this is and why, and
[docs/EVALUATION.md](../../docs/EVALUATION.md) for the benchmark methodology and its limits.

```bash
pip install -e ".[serve,dev]"
pytest                  # 127 tests
ruff check src tests
dialtone --help
```

## Layout

| module | what it owns |
|---|---|
| `turn/endpointing.py` | the adaptive endpointer — silence, syntax, prosody |
| `turn/bargein.py` | interruption vs backchannel; truncating history to played audio |
| `pipeline/orchestrator.py` | the streaming turn loop and its measured budget |
| `flow/graph.py` | the conversation graph, validated before it can load |
| `tools/registry.py` | latency classes, node scoping, idempotency |
| `telephony/provider.py` | the carrier boundary, μ-law, and the call simulator |
| `compliance/redact.py` | streaming redaction of spoken and typed PII |
| `eval/endpointing.py` | the benchmark and the published corpus |
| `sim/call.py` | end-to-end scripted calls |
| `agents/support.py` | a complete worked agent |
| `server/app.py` | HTTP + WebSocket for the studio |

## Wiring a real provider

`TelephonyProvider` is six methods. Implement them against Twilio, Telnyx, or a SIP trunk and
the orchestrator does not change — `SimulatedCall` is a peer of a real provider rather than a
mock of one, which is the only arrangement under which simulator results mean anything.

```python
class TelephonyProvider(Protocol):
    async def answer(self, call_id: str) -> None: ...
    async def hangup(self, call_id: str) -> None: ...
    async def transfer(self, call_id: str, to: str) -> None: ...
    def inbound(self, call_id: str) -> AsyncIterator[SpeechFrame]: ...
    async def send(self, call_id: str, audio: bytes, duration_ms: float) -> None: ...
```

`Recognizer`, `Responder` and `Synthesizer` in `pipeline/orchestrator.py` are the same idea for
the speech services. All three MUST stream: awaiting a complete result at any stage is the
largest avoidable cost in the turn budget, and it is the difference between 610 ms and 2060 ms.
