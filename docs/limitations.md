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
- **Measured on a small sample.** The benchmark is 14 runs (one per scenario) on Claude
  Opus 5.5; it shows the system works end to end, not a tight accuracy estimate.
- **Most commit messages are descriptive.** Four hard variants use terse or misleading
  messages and also passed, but most scenarios still describe their change honestly.
- **Decoys land after the symptoms start**, so timing alone rules them out. A red-herring
  deploy that lands just before the fault is a harder, not yet included test.
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

## Evaluation
- **Scenarios are written by the same person who built the agent.** Mitigations: red herrings,
  multiple variants, answer keys written before any agent run, and scenarios published with
  results.
- **Timing-based alerts are sensitive to host load.** On an 8 GB laptop with other apps open,
  scrapes slowed to 9 s during testing. Benchmark runs should be done with other apps closed.
- **One fault at a time**, with a reset and a quiet period between runs. Overlapping incidents
  are not tested.
- **The v1 baseline is an uncommitted working copy.** Its LLM orchestrator was never pushed to
  the public IncidentPilot repo; results record the snapshot's SHA-256 so the exact code is
  identifiable, but it can't be checked out from a public commit.
- **Proposals are scored, not executed.** Remediation success is shown separately by the
  control plane's live checks; the benchmark measures diagnosis and the proposed fix.
