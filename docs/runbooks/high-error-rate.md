# High error rate (5xx)

**Alerts:** HighErrorRate

## Triage
1. Find which services are failing. Requests flow gateway -> orders -> payments, so an error in a
   downstream service shows up in every service above it. The service furthest downstream that is
   failing is usually closest to the cause.
2. Read that service's error logs. Upstream errors name the dependency that failed
   (for example "payments call failed: 500 from http://payments..."). Follow them down.
3. Check what changed: recent deploys of each affected service, and feature flag changes.
   Compare change times with the time the error rate started rising.
4. A change that happened *after* errors started cannot be the cause.

## Remediation
- Errors started right after a deploy of the failing service: roll back that deploy.
- Errors started right after a feature flag change: set the flag back.
- No change lines up: escalate with the evidence gathered.
