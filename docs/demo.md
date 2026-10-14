# Demo video script (about 3 minutes)

The story: v1 invented evidence when its tools failed. SentinelOps gathers real evidence, cites
it, and can't change anything without a human approving the exact change. Then the numbers.

## Before recording
- Close other apps (the 8 GB laptop needs the memory; see limitations.md).
- `make up`, then `make reset`, and wait until http://localhost:9090/alerts shows nothing firing.
- Start the control plane in its own terminal: `make controlplane`.
- Windows: one terminal (large font), one browser tab on http://localhost:8080.
- Needs `ANTHROPIC_API_KEY` in `.env` for scenes 3 and 6. Without it, use the operator path
  (`make incidents ARGS="open --propose rollback:payments"`) and say so on screen.

## Scenes

**1. The problem (0:00-0:20).** Show the README "Why this exists" table.
> "My first version was a RAG copilot. Its metrics client import failed silently, so the model
> never saw a metric and still wrote 'metric correlations'. SentinelOps is the rebuild that
> fixes that: evidence first, and a human approves every change."

**2. Break production (0:20-0:40).**
```bash
make fault NAME=bad_deploy VARIANT=c
```
> "A bad payments release, and 30 seconds later a harmless gateway deploy. The newest change is
> a red herring." Show the alerts firing on gateway, orders and payments.

**3. The agent investigates (0:40-1:30).**
```bash
make investigate
```
> "It reads alerts, metrics, logs and deploy history through read-only MCP tools. Every claim
> cites the tool call it came from." Open the incident page: point at the claims, click an
> evidence chip (for example T4) to show the raw tool output, and at the cost card.
> "It ignored the most recent deploy because the errors started before it."

**4. Approve the exact change (1:30-2:00).** On the incident page:
> "The proposal is resolved into one exact change: this commit, these settings, secrets
> redacted. My approval is bound to its fingerprint; if anyone deploys in between, execution
> is refused." Type your name, click Approve.

**5. Verify (2:00-2:20).** The page refreshes through EXECUTING and VERIFYING to RESOLVED.
> "It checks the original symptoms on every service that alerted, not just the one it changed.
> If they don't clear, it undoes its own change and escalates."

**6. Safety and numbers (2:20-3:00).**
- Optional quick cut: stop Prometheus (`docker compose -f infra/docker-compose.yml stop
  prometheus`), run `make investigate`, and show the answer listing "metrics unavailable"
  under data gaps instead of invented numbers. Start Prometheus again afterwards.
- Show http://localhost:8080/benchmark (or the README Results table).
> "Ten fault scenarios, three runs each, against v1 on the same alerts. Only measured numbers."
- End on the repo: CI badge, design doc, limitations.

## After recording
`make reset`. Trim long waits (alert onset, verification) in the edit and say the real
durations in a caption rather than speeding them up silently.
