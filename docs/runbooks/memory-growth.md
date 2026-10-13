# Memory growth and OOM kills

**Alerts:** HighMemoryUsage, FrequentRestarts

## Triage
1. Query process_resident_memory_bytes for the service over the last 30-60 minutes. Steady
   growth that never levels off is a leak; a step up after a restart is normal warm-up.
2. Check container events for `oom` followed by `die` with exit code 137 (killed) and `start`.
   `oom_killed_last_exit` in container status resets after the container restarts, so events
   are the reliable record.
3. Look for application metrics that grow alongside memory (caches, queues, connection counts).
4. Check deploys of that service from when growth started; read the diff for settings that
   control caches, buffers or limits.

## Remediation
- Growth started after a deploy: roll back that deploy (fixes the cause).
- Restarting clears memory but the leak returns; use it only as a stopgap.
