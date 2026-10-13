# High latency (p95)

**Alerts:** HighLatencyP95

## Triage
1. Check p95 latency for every service. Each service waits on the one below it, so a slow
   dependency makes everything above it slow by about the same amount.
2. Check error rates. Slow but not failing usually means waiting on something, not a crash.
3. Read logs of the most downstream slow service for slow-call warnings and timeouts.
4. Check recent deploys and flag changes against the time latency started rising.

## Remediation
- Latency started right after one of our own changes: roll it back.
- The time is spent in an external dependency we don't control (for example the payment
  provider) and none of our changes lines up: escalate to the dependency's owner. Rolling back
  unrelated deploys will not help and adds risk.
