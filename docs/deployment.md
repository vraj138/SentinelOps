# Deployment guide

How to run SentinelOps, configure it, and point it at infrastructure other than the bundled
demo stack. Read [limitations.md](limitations.md) alongside this.

## 1. Run it locally (the demo stack)

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), a Docker runtime (OrbStack or
Docker Desktop), about 1 GB of free RAM for the stack. Everything is arm64-compatible.

```bash
cp .env.example .env          # fill in what you need (see section 2)
make install                  # dependencies
make up                       # demo services, Postgres x2, Prometheus, event recorder
make ps                       # all containers healthy?
make controlplane             # approval UI on http://localhost:8080 (keep it running)
```

Break something and handle it:

```bash
make fault NAME=bad_deploy VARIANT=c          # list all with: make faults
make investigate                              # agent investigates, opens an incident (needs a key)
# or, without a key, propose a fix yourself:
make incidents ARGS="open --propose rollback:payments"
# review the evidence and the dry run in the UI, then Approve or Reject
make reset                                    # back to baseline
make down                                     # stop everything
```

Everything an incident did is in `storage/runs/<incident id>/audit.jsonl`: every LLM call
(tokens, cost, timing), every tool call and result, the proposal, the approval, each
verification round and any auto-rollback.

## 2. Configuration

All settings are environment variables (or `.env`, which is git-ignored).

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Claude, the primary model (also read from `ant auth login`) |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | model for investigations |
| `AGENT_EFFORT` | `high` | `low` to `max`; trades depth for tokens |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | none | fallback provider, used only if both are set and Claude is unavailable |
| `AGENT_MAX_TOOL_CALLS` | `30` | per investigation; then the model must answer from what it has |
| `AGENT_MAX_TOKENS` | `1000000` | per investigation; then it escalates |
| `AGENT_MAX_COST_USD` | `3.0` | per investigation; then it escalates |
| `AGENT_MAX_SECONDS` | `600` | per investigation; then it escalates |
| `PROMETHEUS_URL` | `http://localhost:9090` | metrics and alerts |
| `CONTROLPLANE_DATABASE_URL` | Postgres on `localhost:5433` | incident state (not the demo app's database) |
| `CONTROLPLANE_URL` | `http://localhost:8080` | used in links printed by the CLI |
| `DEPLOY_REPO_PATH` | `storage/deploy-repo` | git repo of release settings ("what changed") |
| `FLAGS_PATH` | `storage/flags/flags.json` | feature flags file |
| `CONTAINER_EVENTS_PATH` | `storage/events/containers.jsonl` | container lifecycle log |
| `RUNBOOKS_DIR` | `docs/runbooks` | markdown runbooks the agent can search |
| `DOCKER_BIN` | `docker` | full path if docker is not on `PATH` |
| `CONTAINER_NAME_TEMPLATE` | `sentinelops-{service}-1` | maps a service name to its container |
| `RUNS_DIR` | `storage/runs` | audit logs and results |

## 3. Pointing it at your own environment

SentinelOps reaches infrastructure only through small adapters. To deploy it somewhere new,
configure or replace one adapter at a time. Start read-only: the agent can investigate long
before any remediation is enabled.

| Need | Today | To adapt |
|---|---|---|
| Metrics and alerts | Prometheus HTTP API (`mcp_servers/metrics.py`) | Any PromQL-compatible endpoint works by setting `PROMETHEUS_URL`. Update the metric names in the server's `instructions` and the verifier's queries (`sentinelops/controlplane/verify.py`) to match your labels. |
| Container state and logs | docker CLI (`mcp_servers/docker_backend.py`) | Implement the same three reads (`status`, `logs`, `events`) for your runtime, such as the Kubernetes API, and keep the field whitelist so environment variables are never read. |
| Change history | a git repo of release settings (`mcp_servers/changes.py`) | Point `DEPLOY_REPO_PATH` at a GitOps repo, or implement `deploys` and `show` against your deploy system's API. |
| Feature flags | a JSON file | Implement `flags` against your flag service. |
| Runbooks | markdown in a folder | Point `RUNBOOKS_DIR` at your runbooks; keep them procedural, not tied to past incidents. |
| Services in scope | `gateway`, `orders`, `payments` | The allowlist in `mcp_servers/common.py` (`SERVICES`) and `sentinelops/controlplane/actions.py`. |
| Remediation | deploy repo, flags file, `docker compose` (`sentinelops/stack/`) | Implement the `Runtime` protocol (recreate, restart) and the release write for your platform. Leave this until diagnosis is trusted. |

Each MCP server runs as its own process (`python -m mcp_servers.<name>`) over stdio, so a server
can be moved closer to the system it reads without changing the agent.

## 4. Security posture

- **Read-only diagnostics.** Investigation tools have no write paths; the policy gate allows only
  an explicit list of read-only tools, and each must also be declared read-only by its server.
- **No free-form commands.** Remediation is three typed actions; there is no shell tool.
- **Human approval bound to the exact change.** The approval records who, when, and a fingerprint
  of the resolved action; execution re-checks the fingerprint and refuses if anything changed.
  The approval form must echo the fingerprint, which blocks forged cross-site approvals.
- **Secrets.** Tool results, logs, diffs, the incident API and the audit log mask values of
  secret-looking keys (`*KEY*`, `*SECRET*`, `*PASSWORD*`, `*TOKEN*`) and URL credentials.
  Container inspection selects specific fields and never reads environment variables.
- **Not production-ready auth.** The control plane has no login or RBAC; keep it on localhost
  or behind an authenticating proxy.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker: command not found` from tools | Docker CLI not on `PATH` for the subprocess (OrbStack installs to `~/.orbstack/bin`) | set `DOCKER_BIN` to the full path, or open a new terminal |
| `no Anthropic credentials found` | no key in `.env` or environment | set `ANTHROPIC_API_KEY` or run `ant auth login` |
| Every service briefly "down", scrapes take seconds | the host is out of memory (seen with ~100 MB free and heavy swap) | close other apps; the stack itself needs about 400 MB |
| A memory leak swaps instead of being OOM-killed | swap enabled for the container | demo containers set `memswap_limit` equal to `mem_limit` |
| OOM kills missing from container events | Docker keeps only 256 events and health checks flood them | the `event-recorder` container persists lifecycle events; check it is running |
| Old alerts linger after recreating Prometheus | Compose keeps anonymous volumes on recreate | recreate with `--renew-anon-volumes` (the benchmark's clean slate does) |
| `make reset` leaves something changed | reset restores releases, flags and the provider; it does not clear logs or events | the benchmark's clean slate (`sentinelops/evals/cleanslate.py`) does a full reset |

## 6. Running the benchmark

See the README's Results section and [design doc section 7](design.md#7-evaluation-plan).
`make bench-dry` is free and checks the harness. Paid steps all require `--confirm-spend`.
