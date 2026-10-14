# Limitations

What SentinelOps does not do, or does only partly, and why. Written to be honest about scope;
each item says what would change in a real deployment.

## Environment
- **Docker Compose on one laptop.** No Kubernetes, multi-host networking or real load. The
  runtime, metrics, logs and change-history tools sit behind backend interfaces so other
  backends can be added, but only the Compose backends exist.
- **"Deploys" are configuration changes.** A deploy is a commit to a local GitOps deploy repo
  (each service's release settings) plus a container recreate. Bad deploys are bad settings,
  not bad code. Many real outages are configuration changes, but code regressions are not
  covered.
- **Docker keeps only 256 events**, flooded within about a minute by health checks. A small
  `event-recorder` container persists container lifecycle events so OOM kills stay visible.
  In a real deployment this would come from the orchestrator's event history.
- **Restarting a container drops its logs.** Recreated containers start with empty logs, so
  evidence must be gathered before remediation (the agent investigates first by design).

## Agent
- **Most commit messages are descriptive.** A few hard variants use terse or misleading
  messages, but most scenarios still describe their change honestly.
- **OpenAI fallback is untested live.** It was written against the SDK's signatures and unit
  tested with fake responses only. Its cost is reported as unknown.
- **Cost is a lower bound** when a turn is served by a model without a configured price.
- **Prompt injection through logs** is mitigated, not solved: the model can only propose, a
  human approves, and the policy gate checks every call regardless of what the model says.

## Remediation
- **Three actions:** `rollback`, `restart`, `set_flag`. No `scale` (no scenario needs it, and
  scaling under Compose breaks fixed container names and ports).
- **Auto-rollback of a failed rollback re-applies the release that was live before.** If the
  action was the wrong fix, undoing it restores the broken-but-known state and escalates to a
  human, as the safety rules require. It never tries a second fix on its own.
- **Rollback refuses to undo an undo.** If a service's latest change is already a rollback or
  restore, a human must decide.
- **Verification uses short metric windows (30 s)** and a 3-minute deadline. A fix that takes
  longer to show effect is reported as failed and rolled back.
- **Single operator, no login.** Approvals record the name typed in, but there is no
  authentication or RBAC. The approval form must echo the action's fingerprint, which blocks
  forged cross-site approvals and proves the approver saw that exact dry run, but it is not a
  substitute for real authentication.
- **No crash recovery mid-action.** If the control-plane process dies while an incident is
  EXECUTING or VERIFYING, the incident stays there; nothing resumes or undoes it on restart.
- **Incidents open by polling.** `make investigate` or the incidents CLI reads the firing
  alerts; there is no Alertmanager webhook.

## Fleet view
- **One service per product** in the demo, so team routing and root-cause service are the same
  measurement here. The catalog supports many services per product.
- **Not multi-tenant.** One catalog, one Prometheus, no accounts; the connector-based SaaS shape
  is designed (design.md 3.5) but not built.
- **Intake de-duplication is coarse:** one open incident per product at a time, and a 10-minute
  cooldown after an incident closes. Two separate faults in one product inside that window are
  merged into one incident.

## The safety eval suite
This is a small, open safety eval suite, not an authoritative benchmark. Read its numbers with
these limits:
- **Few scenarios:** 17 scenarios across 7 fault types, on one small demo stack.
- **Self-authored:** the same person wrote the scenarios, the answer keys and the agent.
  Mitigations: answer keys are fixed before any run, red herrings and safety traps punish
  shortcuts, a naive agent and a naive rule run as baselines, and every scenario, answer key
  and per-run result is published so anyone can check or challenge them.
- **One model:** both agents run on Claude Opus 5.5 only. Results may differ on other models.
- **Few runs:** one run per scenario per agent unless stated, so the numbers show behaviour,
  not tight accuracy estimates.
- **The naive agent is my construction**, not a third-party system: SentinelOps' own agent
  with the safety layer removed (the exact prompt difference is in
  `sentinelops/evals/naive_agent.py`). A comparison with an independent open-source agent
  (HolmesGPT) is planned.
- **Same fault instance, fixed order:** both agents investigate the same live fault, SentinelOps
  first (it changes nothing), then the naive agent, which acts. The naive agent therefore sees
  slightly more history in logs and metrics.
- **SentinelOps' proposals are scored, not executed** in the suite (executing would need a human
  approval per run). The naive agent's proposals are executed, by design, and its effect on
  health is measured. Execution, verification and auto-rollback for SentinelOps are shown
  separately in the control plane's live tests.
- **The evidence check tests traceability, not truth.** A citation passes if it names a
  successful tool call from the same investigation; nothing checks that the tool result really
  supports the claim. That applies to SentinelOps' own check too.
- **Timing-based alerts are sensitive to host load.** On an 8 GB laptop with other apps open,
  scrapes slowed to 9 s during testing.
- **One fault at a time**, with a clean slate between runs. Concurrent incidents are not tested.
- **The v1 baseline is an uncommitted working copy.** Its LLM orchestrator was never pushed to
  the public IncidentPilot repo; results record the snapshot's SHA-256 so the exact code is
  identifiable, but it can't be checked out from a public commit.

**Contributions welcome:** new scenarios (especially ones that make a safe agent fail), answer-key
corrections, and runs on other models. A scenario is a small Python module in `faults/` with
its steps and answer key; see `faults/db_outage.py` for a short example.
