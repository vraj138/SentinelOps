# Feature flag changes

## Triage
1. List current feature flags: who changed them last and when.
2. Services log every flag change: "feature flag <name> changed False -> True (updated_by=...)".
   Compare the change time with when errors or latency started.
3. A flag change is a change like a deploy, but it does not restart anything or change the
   service version, so deploy history alone will miss it.

## Remediation
- Problems began right after a flag was turned on: set the flag back to its previous value.
