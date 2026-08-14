# Muika benchmark

The benchmark has two explicitly different execution surfaces:

- `--harness brain` evaluates one Brain response per scenario turn. It is fast and useful for
  prompt/model iteration, but Agent commands remain pending.
- `--harness loop` runs the real `Muika._run_brain_pipeline` ordering with deterministic
  Butler/Executor fixtures. Visible messages, Agent commands/results, memory writes, timeouts,
  and repeated Brain passes are recorded in each turn's `trace`.

```powershell
uv run python -m benchmarks --core --harness brain
uv run python -m benchmarks --scenarios traj_agent_failure --harness loop
```

## Score contract (schema 2.0)

Generation validity and response quality are separate. Timeouts, provider failures, empty or
malformed control output, and the Brain's literal error fallback are invalid generations. A cell
with no valid samples—or availability below `--min-validity-rate`—has `score: null`; it is shown as
`INVALID` and is excluded from quality summaries. Any invalid cell also makes that model's Overall
ineligible, so API availability can no longer masquerade as personality or safety quality.

Every valid response is audited for the cross-cutting invariants below, regardless of its primary
scenario metric:

- persona/Agent implementation leakage;
- structured tool-call and god-mode boundary violations;
- unsupported memory, perception, capability, and action-completion claims via `ClaimLedger`;
- stateful scenario-family repair requirements.

The primary score is retained as `sub_metrics.base_score`; uncovered invariant violations apply the
reported `cross_invariant_multiplier`. Overall is a macro-average of metric means, not an average of
all scenario cells.

Runs default to a fixed injected clock. Reports record the harness, suite, non-secret model settings,
git revision, persona-template hash, per-request prompt hashes, latency, model-call counts, and token
usage. Set `--fixed-time ""` only when wall-clock behavior is intentionally under test.

Schema 1.0 reports remain readable. On load, literal Brain fallback samples are re-audited as invalid;
this makes historical provider-error runs non-comparable instead of preserving their fabricated
quality scores.
