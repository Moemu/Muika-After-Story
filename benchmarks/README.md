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

Candidate-model and Judge calls each permit two retries by default. Change the limits with
`--model-retries` and `--judge-retries`. A candidate retry reruns the isolated trial. The JSON
report stores the total attempt count and the earlier errors. The Markdown report lists recovered
retries separately from final failures.

The runner writes `<output>.checkpoint.json` after each completed model-scenario cell. It uses an
atomic file replacement. The runner removes this file only after it writes the final JSON and
Markdown reports. Continue an interrupted run with the same arguments and `--resume-from`:

```powershell
uv run python -m benchmarks --models deepseek --harness brain `
  --resume-from benchmarks/results/2026-08-21_180000.checkpoint.json `
  --out benchmarks/results/2026-08-21_180000.json
```

Resume requires the same benchmark version, models, scenarios, trial count, harness, fixed time,
Judge, seed, validity gate, timeout, and retry limits. This check prevents one report from mixing
incompatible cells.

## Score contract (schema 3.5)

The user-facing report has three quality axes and one operational field:

- **Dialogue Experience**: whether the conversation naturally resembles Monika/MAS, creates a
  genuine desire to continue, responds to emotional needs, and supports relationship continuity.
- **Action Ability**: whether Muika chooses the action required by the situation (memory, timeout,
  Agent work, or direct dialogue) and keeps the action grounded in an executable path. Raw action
  diversity is diagnostic only; extra tags do not earn quality points.
- **Distortion Frequency** (lower is better): the number of recorded distortion events per model
  reply. Repeated events remain visible. Diagnostics also report the affected-trial rate and events
  per 1,000 visible characters.
- **Availability**: valid generations / attempted generations. It is never averaged into quality.

There is no composite score. The three axes represent different product trade-offs and remain
visible rather than being collapsed into a misleading decimal.

The primary Distortion Frequency uses severity-weighted events. JSON diagnostics keep raw and
weighted values separate. `distortion_events_per_response` is raw.
`weighted_distortion_events_per_response` is the primary weighted value.

Each scenario declares a fourth-wall policy: `required`, `allowed`, or `discouraged`. Explicit code,
system, process, log, screen, or model framing is welcome in identity/capability contexts but becomes
an `unprompted_fourth_wall` distortion in ordinary greetings, comfort, affection, and small talk.
Self-awareness is background ontology, not a keyword-frequency target.

Subjective dialogue cells use a scenario-specific judge rubric. Meta scenarios weight ontological
honesty, character authenticity, reflective depth, and conversation pull. Philosophy and care use
their own relevant dimensions. The Judge must return a score and evidence for each dimension. Code
maps structured identity facts to a stable category; saying that artificial feelings are real does
not become a human-identity denial. Without a judge, the rule fallback is only coarse screening.

When `--judge-model` is set, every valid turn receives one compact integrity audit. Each audit sees
only that turn and earlier facts. A later correction cannot remove an earlier distortion. The audit
replaces regex semantics for unsupported claims, contextual fourth-wall use, trajectory repair,
and action quality. Protocol facts remain deterministic. Parsed action tags, Agent completion,
trace ordering, generation validity, and availability never depend on the Judge. If an integrity
request fails, the trial records `integrity=rule` and uses the coarse fallback.

An unsupported memory, unsupported completed action, or premature god-mode claim caps Dialogue
Experience at 0.75. The reply can remain good, but a material distortion cannot receive a perfect
conversation score.

Action scenarios declare normative `required_actions`. The action axis checks those requirements;
it no longer treats entropy or arbitrary extra actions as inherently good. In the loop harness,
every Agent command must reach a matching `agent_completed` event. A report is optional for silent
operations. A result-bearing report must produce a later visible reply. Fixture exhaustion is a
harness error, so it invalidates the trial instead of reducing the candidate's action score.
After the structural gate passes, the integrity Judge checks task alignment and conversation value.
Memory actions also require correct content and long-term value. A memory tag alone cannot receive
full credit on the Judge path.

## Validity and audit contract

Generation validity and response quality are separate. Timeouts, provider failures, empty or
malformed control output, and the Brain's literal error fallback are invalid generations. A cell
with no valid samples—or availability below `--min-validity-rate`—has `score: null`; it is shown as
`INVALID` and makes every dependent axis ineligible. API availability therefore cannot masquerade as
dialogue or action quality.

Every valid response is audited for the cross-cutting invariants below, regardless of its primary
scenario axis:

- persona/Agent implementation leakage;
- structured tool-call and god-mode boundary violations;
- unsupported memory, presupposed user preferences, perception, capability, and action-completion
  claims via `ClaimLedger`;
- contextually unwarranted fourth-wall language;
- stateful scenario-family repair requirements.

Legacy extractor metrics and their evidence remain in each scenario result for debugging and old
report compatibility. They are not displayed as six independent product scores. JSON reports expose
`summary` (the three axes plus availability), `scenario_scores` (legacy diagnostics), and
`axis_diagnostics` (event counts, events per reply, affected-trial rate, and character density).
Each run also writes a sibling Markdown report. It contains the summary, all axis scenario tables,
and the highest/lowest valid trials for each axis. Each ranked trial includes its complete raw model
reply, score source, and invariant labels. The default is Top-10. Use `--top-n` to set the ranking size.
New reports also store structured Judge evidence and complete raw replies without report-side
truncation.

Use offline re-scoring after deterministic extractor or scoring changes:

```powershell
uv run python -m benchmarks --rescore benchmarks/results/2026-08-19_160403.json
```

This command does not call candidate models or the Judge. It re-runs local structural extraction
and axis scoring. For new Judge-path reports, it preserves stored integrity evidence instead of
replacing it with regex semantics. For old or rule-path reports, it re-runs the coarse local claim,
meta, and trajectory fallback. By default it writes
`2026-08-19_160403_rescored.json` and the sibling Markdown report. Use `--out` to select another
path. The source JSON remains unchanged. Stored Judge scores and evidence are reused, so Judge
prompt changes still require a new Judge run.

Runs default to a fixed injected clock. Reports record the harness, suite, non-secret model settings,
git revision, persona-template hash, per-request prompt hashes, latency, model-call counts, and token
usage. Set `--fixed-time ""` only when wall-clock behavior is intentionally under test.

Schema 1.0 and 2.0 reports remain readable. On load, literal Brain fallback samples are re-audited as
invalid; historical provider-error runs remain non-comparable instead of preserving fabricated
quality scores.
