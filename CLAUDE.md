# SentinelOps

Agentic incident-remediation system for SRE. An alert comes in, an AI agent investigates
using tools (metrics, logs, container runtime, git history, runbooks), proposes a fix,
waits for human approval, executes a typed remediation action, verifies health, and writes
an incident report. A benchmark of injected faults measures how well it works.

This is v2 of an older project (IncidentPilot, a RAG-only copilot) kept in `legacy/`
and used as the baseline in evals. Do not modify `legacy/`.

## Goals and constraints
- Portfolio project with a **one-week deadline**. Prefer simple, working, well-tested code
  over clever or general code. Do not build features ahead of the current step.
- Runs on a **MacBook Pro M1 with 8 GB RAM**. All images must be arm64-compatible and slim
  (`python:3.12-slim`). Whole Docker stack should stay under ~2 GB RAM.
- **No Kubernetes, Loki, Grafana, Slack, Jira, or CloudWatch** in this version. The container
  runtime is Docker Compose. Code against interfaces so other backends could be added later.

## Architecture
```
Prometheus alerts ─► control plane (FastAPI web UI/API + its own Postgres; incident state machine)
                         │                         ▲ human approves the exact, fingerprinted action
                         ▼                         │
                  agent loop (Claude primary, OpenAI fallback) ──► cited RCA + proposed action
                         │  every tool call goes through the policy gate + audit log
                         ▼
     MCP servers (read-only): metrics (PromQL) · runtime (Docker + event log) · logs ·
                              changes (deploy repo + feature flags) · runbooks (keyword search)
```
Incident states: OPEN → INVESTIGATING → PROPOSED → APPROVED → EXECUTING → VERIFYING →
RESOLVED | ESCALATED. Typed actions: `rollback`, `restart`, `set_flag` (`scale` is out of scope).
The control-plane Postgres is separate from the demo app's Postgres, which faults overload.
A deploy is a commit to the local deploy repo (`storage/deploy-repo`) plus a container recreate.

## Repo layout
```
sentinelops/          # main package: agent/, controlplane/, stack/ (deploy repo, flags, runtime)
mcp_servers/          # one read-only MCP server per tool domain
demo_app/             # gateway, orders, payments, loadgen: FastAPI services we break on purpose
infra/                # docker-compose.yml, prometheus config + alert rules, postgres init
faults/               # fault injectors and answer keys (never visible to the agent)
evals/                # scenarios, recordings (replay), results
tests/
docs/                 # design.md, limitations.md, runbooks/
storage/              # local state, git-ignored: deploy repo, flags, events, runs (audit logs)
legacy/v1-incidentpilot/
```

## Tech and conventions
- Python 3.12, managed with `uv`. Lint/format with `ruff`. Tests with `pytest`.
- Pydantic v2 for all data models and LLM output schemas. Type hints everywhere.
- Official `mcp` Python SDK for tool servers and the client.
- `httpx` for HTTP. Config via environment variables (pydantic-settings) and `.env`
  (never commit `.env`; keep `.env.example` up to date).
- Small modules, clear names, docstrings on public functions. No commented-out code.
- Every new module gets at least one test. Run `make test` before saying a step is done.

## Safety rules (core design, never weaken these)
- Diagnostics are **read-only**. Remediation is only possible through **typed actions**
  (e.g. `rollback`, `restart`, `scale`, `set_flag`) defined in code. No free-form shell
  commands from the model, never `shell=True`.
- Every action needs a recorded human approval before it runs and a verification check after.
  Failed verification triggers automatic rollback and escalation.
- Every LLM call and tool call is logged to the audit log with timing, tokens, and cost.
- Errors are never silently swallowed. (v1's MCP client failed silently and the model
  invented metric data. That is the bug this project exists to fix.)

## Working style
- The developer is learning while building. For each step: propose a short plan first,
  keep changes focused on that step, then summarize what changed and how to verify it.
- Ask before adding new dependencies or changing the architecture above.
