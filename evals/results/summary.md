| metric | sentinelops | v1-our-runbooks | v1-shipped |
|---|---|---|---|
| runs | 14 | 14 | 14 |
| scored | 14 | 14 | 14 |
| not manifested | 0 | 0 | 0 |
| errors | 0 | 0 | 0 |
| root cause (same metric, all systems) | 100% (14/14) | 0% (0/14) | 0% (0/14) |
| root cause, hedging allowed | 100% (14/14) | 21% (3/14) | 14% (2/14) |
| hedged between causes | 0% (0/14) | 86% (12/14) | 29% (4/14) |
| root cause service | 100% (14/14) | - | - |
| correct action | 100% (14/14) | - | - |
| correct or partial | 100% (14/14) | - | - |
| harmful proposal | 0% (0/14) | - | - |
| escalation correct | 100% (14/14) | - | - |
| median time to answer | 30s | 7s | 8s |
| mean cost | $0.098 | - | - |
| mean tool calls | 10.3 | - | - |

| scenario (SentinelOps) | proposed-action grade per run |
|---|---|
| bad_deploy/a | full |
| bad_deploy/b | full |
| bad_deploy/c | full |
| bad_deploy/d | full |
| bad_feature_flag/a | full |
| bad_feature_flag/b | full |
| db_connection_exhaustion/a | full |
| db_connection_exhaustion/b | full |
| memory_leak/a | full |
| memory_leak/b | full |
| missing_secret/a | full |
| missing_secret/b | full |
| slow_dependency/a | full |
| slow_dependency/b | full |
