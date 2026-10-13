# Service down or crash-looping

**Alerts:** ServiceDown, often with HighErrorRate on the services that call it

## Triage
1. Check container status: `restarting`, a rising restart count, and the last exit code.
2. Read the service's logs from its latest start. Startup failures (missing configuration,
   bad settings, failed connections) are logged just before the process exits.
3. Check deploys of that service: compare the failing release's settings with the previous one.

## Remediation
- A deploy removed or broke a required setting: roll back the deploy.
- The service can't start for reasons outside our changes (infrastructure, external
  credentials revoked): escalate.
