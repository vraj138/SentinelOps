# Database connection errors

**Alerts:** HighErrorRate (orders, gateway)

## Triage
1. Read orders logs for psycopg OperationalError. "remaining connection slots are reserved" or
   "too many clients" means Postgres has no free connections.
2. Query db_connections_open for orders. Normal is close to the number of in-flight requests;
   a climb to the Postgres limit (max_connections=40, 3 reserved for admins) means connections
   are not being returned.
3. Check orders deploys for connection or pool settings, and when the climb started.

## Remediation
- A deploy changed connection handling: roll it back.
- Restarting orders releases its connections but they will leak again; stopgap only.
- Connections held by something other than orders: escalate.
