# SentinelOps design

**Status:** living document. The demo environment (§3.1) is built and verified. Everything else
describes the planned design and will be updated as each part is built.

---

## 1. Problem and goals

When an alert fires, an on-call engineer has to answer three questions under pressure: *what is
broken, why, and what is the safest fix?* Most of the work is collecting evidence: dashboards,
logs, recent deploys, runbooks. That part can be automated. Deciding to change production should
stay with a human.

The first version of this project, IncidentPilot, tried to help with a RAG copilot. Its MCP
client failed to import, the exception was swallowed, and the model invented metric data that
never existed. **An assistant that sounds right but has no evidence is worse than no assistant.**
That failure shapes every decision below.

**Goals**
1. **Evidence-based diagnosis.** The agent investigates with real tools and produces a root-cause
   analysis (RCA) in which every claim cites the tool calls that support it.
2. **Safe remediation.** Fixes come only from a small set of typed actions. Each one needs human
   approval, is verified after it runs, and rolls back automatically if verification fails.
3. **Fail loudly.** Every error is surfaced. A missing tool result is reported as missing, never
   filled in by the model.
4. **Measured, not claimed.** A fault-injection benchmark scores the system against the v1
   baseline, with repeated runs and a published method.
5. **Portable.** Infrastructure is reached through config-driven adapters, so the same agent can
   be pointed at a different environment without code changes to the agent itself.

**What counts as success:** on the benchmark, SentinelOps finds the correct root cause more often
than v1, takes no harmful actions, and escalates when it should, at a known cost per incident.

## 2. Non-goals

| Not in this version | Why |
|---|---|
| Kubernetes | The target machine is an 8 GB laptop, and a cluster plus an observability stack won't fit. The runtime adapter interface keeps Kubernetes possible later. |
| Loki, Grafana, CloudWatch | Logs come straight from the Docker API, and Prometheus covers metrics. Fewer moving parts. |
| Slack, Jira, paging | Approval happens in the SentinelOps UI. Chat-ops is a roadmap item. |
| Full RBAC and multi-tenant auth | There is a single operator. Approvals still record who approved. |
| Fully autonomous remediation | Out of scope by design. A human always approves. |
| Free-form shell or code execution by the model | Never, in any version (see §4). |

## 3. Architecture

```
                 ┌──────────────────────── demo environment (Docker Compose) ───────────────────┐
                 │  loadgen → gateway → orders → payments        Postgres        Prometheus      │
                 └──────────────────────────────────────────────────────────────────┬───────────┘
                                                                                    │ alert
                                                                                    ▼
┌─────────────────────────────── SentinelOps ─────────────────────────────────────────────────┐
│ Control plane (FastAPI + Postgres)   incident state machine · approvals · audit log          │
│        │                                                                                     │
│        ▼                                                                                     │
│ Agent loop (Claude primary, OpenAI fallback)   budgets · timeouts · structured output        │
│        │  every call                                                                         │
│        ▼                                                                                     │
│ Policy gate ── is this tool allowed in this state, with these arguments? ── audit log        │
│        │                                                                                     │
│        ▼                                                                                     │
│ MCP servers:  runtime │ metrics │ logs │ git │ runbooks          Typed actions (separate)    │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Demo environment (built)

The environment that gets broken on purpose. It is part of the repo so the benchmark is
reproducible.

- **gateway → orders → payments** are three FastAPI services; orders writes to Postgres.
  **loadgen** sends about 3 checkouts per second.
- Every service exposes `/health` and `/metrics`:
  - `http_requests_total{service,route,status}`
  - `http_request_duration_seconds{service,route}`
  - `app_build_info{service,version}`
- **Deploys are observable.** Images are tagged with the git commit, and `app_build_info` reports
  it. A deploy is a real commit plus a new tag, so "what changed recently?" has a factual answer.
- **Failures carry their cause.** An upstream failure becomes a 502 whose body names the failing
  dependency, and the error is logged. The stack never hides evidence the agent will need.
- **Prometheus** scrapes every 5 s. Starter rules are `ServiceDown`, `HighErrorRate` (>5% 5xx for
  1 min) and `HighLatencyP95` (>1 s for 1 min).
- **Memory limits** on every container keep the stack at about 365 MB at idle. They also make the
  out-of-memory fault realistic, because a leak hits a real limit and the container is
  OOM-killed.

### 3.2 Incident lifecycle

```
OPEN → INVESTIGATING → PROPOSED → APPROVED → EXECUTING → VERIFYING → RESOLVED
                 │          │          (rejected)            │
                 └──────────┴──────────────► ESCALATED ◄─────┘ (verification failed → rollback)
```

| State | Entered when | What may happen |
|---|---|---|
| OPEN | An alert arrives and is not part of an open incident | Nothing; it is queued |
| INVESTIGATING | The agent picks it up | Read-only tool calls only |
| PROPOSED | The agent returns an RCA and a proposed action | A human reviews the proposal |
| APPROVED | A human approves; the approval is recorded | Nothing runs yet |
| EXECUTING | The control plane runs the approved action | Only that action, with those exact arguments |
| VERIFYING | The action finished | Health checks only |
| RESOLVED | Verification passed | A report is written |
| ESCALATED | Budget exhausted, low confidence, rejection, or failed verification after rollback | A report is written with everything gathered so far |

State transitions are enforced in the control plane, not by the model. The model can only *ask*
for the next step; the state machine decides whether that step is allowed.

**Alert intake (planned):** Alertmanager sends a webhook to the control plane. Alerts that arrive
close together are grouped into one incident (see §8, alert cascades).

## 4. Safety design

The model is treated as an **untrusted planner**: useful for reasoning, never trusted with direct
access. Safety comes from structure that the model cannot bypass.

| Control | Protects against | How it's enforced |
|---|---|---|
| Read-only diagnostics | Investigation changing production | The diagnostic MCP servers expose only read operations. Write paths are not in that code at all. |
| Typed actions only | Arbitrary or destructive commands | Actions are Pydantic models (`rollback`, `restart`, `scale`, `set_flag`) with validated arguments. There is no shell tool and never `shell=True`. |
| Policy gate | Calling the right tool at the wrong time, or with unsafe arguments | Every tool call is checked against the incident state and argument limits (for example, scaling within set bounds) before it runs. |
| Human approval | The agent acting on a wrong diagnosis | Planned: the approver, time, and a hash of the exact action payload are recorded. Execution refuses if the payload differs from what was approved. |
| Dry run | Surprises at execution time | Each action describes what it will change before approval. |
| Verification and auto-rollback | A fix that doesn't work or makes things worse | Each action defines a PromQL health check. If it fails, the action's inverse runs and the incident escalates. |
| Audit log | Untraceable decisions | Every LLM call and tool call is recorded with inputs, outputs, timing, tokens and cost. |
| Fail loudly | v1's invented data | Tool errors go back to the model as explicit errors, and the RCA must not cite a failed call as evidence. |

**Prompt injection.** Logs and runbooks are untrusted text, and a log line could contain
instructions. Defenses:
1. The model cannot execute anything by itself. At worst, injected text changes a *proposal*,
   which a human still reviews.
2. Tool output is passed to the model as data, clearly marked, never as instructions.
3. The policy gate checks arguments no matter what the model was told.

## 5. Tool layer

**Why MCP:** the Model Context Protocol gives every tool one standard interface. Each tool
domain becomes a separate, testable server that could be reused by another MCP client. The
official Python SDK is used for both the servers and the client.

| Server | Purpose (read-only) | Backend now | Could be swapped for |
|---|---|---|---|
| runtime | Container status, restarts, OOM kills, image tags | Docker API | Kubernetes API |
| metrics | PromQL queries and current alerts | Prometheus HTTP API | Any PromQL-compatible store |
| logs | Recent logs per service, filtered and capped | Docker logs | Loki, CloudWatch |
| git | Recent commits and diffs for deployed versions | Local git repo | GitHub API |
| runbooks | Search operational runbooks | RAG over markdown (reused from v1, with the v1 bugs fixed) | Any document store |

**Adapter rule:** each server talks to its backend through a small interface (for example
`RuntimeBackend`), and the backend is chosen by configuration. Adding Kubernetes means writing a
new backend, not changing the agent.

**Output rules:** tool results are size-capped and summarized where large, such as logs. Every
result carries a `tool_call_id` that the RCA can cite.

**Remediation actions are not MCP diagnostic tools.** They live in the control plane, are run by
the control plane after approval, and are never called directly by the model.

## 6. Agent loop

**Why hand-rolled instead of a framework:** the loop is short, and every part of it needs to be
controlled and explained: budgets, timeouts, retries, tracing, provider fallback, and exactly
what goes into the audit log. A framework would hide the parts that matter most here.

**The loop (planned):**
1. Build context from the alert, the incident state, and the available tools.
2. The model either calls a tool or returns its final answer.
3. Each tool call goes through the policy gate, then MCP, then the audit log. The result (or the
   explicit error) goes back to the model with its `tool_call_id`.
4. Repeat until the model returns a structured RCA or a budget runs out.

**Structured output:** the final answer is a Pydantic model containing:
- a root-cause summary;
- a list of claims, each with the `tool_call_id`s that support it;
- confidence;
- a proposed typed action, or a recommendation to escalate.

Validation rejects claims that cite unknown or failed tool calls. On a validation failure the
model gets one repair attempt. After that, the incident escalates.

**Budgets:** maximum tool calls, maximum tokens, and maximum wall-clock time per incident. When
any one is exhausted, the incident escalates with the partial evidence gathered so far. A
timeout produces a report, never a guess.

**Model fallback:** Claude is the primary model and OpenAI the fallback, behind one provider
interface. Fallback happens on provider errors or timeouts, and is recorded in the audit log so
the benchmark can report it.

## 7. Evaluation plan

**Fault scenarios:** six fault types with two or three variants each.

| Fault | Expected signal | Correct remediation |
|---|---|---|
| Bad deploy (5xx) | Error rate rises right after a version change | `rollback` |
| Memory leak | Memory climbs, OOM kills, restarts | `rollback` or `restart` |
| DB connection exhaustion | Orders errors, Postgres connection errors in logs | `restart` or `set_flag` |
| Slow dependency | p95 latency rises across the chain | Depends on the variant |
| Bad feature flag | Errors start right after a flag change | `set_flag` |
| Missing secret | Crash loop at startup | Escalate (it needs a human) |

At least one variant of each type includes a **red herring**: an unrelated deploy around the
same time. That tests whether the agent reasons from evidence or blames the most recent change.

**Record and replay:** live runs record every tool response. Replay mode re-runs the agent
against those recordings without the stack running. Replay is cheap and repeatable, so it also
serves as a regression test.

**Metrics:**
- root-cause accuracy;
- correct-remediation rate;
- harmful or unnecessary action rate;
- correct-escalation rate;
- time to proposal;
- tokens, cost and tool calls per incident.

**Baseline:** v1 IncidentPilot gets the same scenarios. It cannot act, so it is scored on root
cause only. Each scenario runs three times, and results are reported with run counts.

## 8. Risks and open questions

- **Alert cascades.** Observed in the demo stack: stopping payments fired `ServiceDown` for
  payments within about 35 s. Gateway and orders also went into `HighErrorRate` pending, at 62%
  and 58% errors, because they depend on payments. The loudest alerts were symptoms, not the
  cause. Plan: group nearby alerts into one incident, give the agent the dependency graph, and
  test this explicitly in the benchmark.
- **Wrong but confident diagnoses.** These are mitigated by required citations, human approval,
  and verification. Open question: should low confidence force escalation instead of a
  proposal?
- **Benchmark validity.** The scenarios are written by the same person who builds the agent.
  Mitigations: red herrings, variants, ground truth fixed before the agent runs, and publishing
  the scenarios with the results.
- **Cost and latency.** Budgets cap the worst case. Actual cost per incident is measured, not
  estimated.
- **Rollback is not always safe**, for example after a database migration. Each action declares
  whether it can be reversed; irreversible actions are out of scope for this version.
- **Open:** Alertmanager webhook or polling Prometheus? The plan is the webhook, because it's the
  standard integration point.

## 9. Decisions log

| Decision | Alternatives considered | Reason |
|---|---|---|
| Docker Compose | Kubernetes (kind, k3d) | Fits 8 GB RAM; adapters keep Kubernetes possible |
| Hand-rolled agent loop | LangGraph, other agent frameworks | Full control of budgets, fallback and audit; easy to explain |
| MCP for tools | Direct function calls | Standard interface, separable servers, reusable |
| Typed actions with approval | Model-generated commands | Safety is enforced by structure, not by prompting |
| Replay from day one | Live-only evals | Cheap, repeatable runs and regression tests |
| v1 as the baseline | No baseline | Shows measured improvement against a real earlier system |
| One image for all demo services | One image per service | Faster builds, less RAM, the same code paths |
