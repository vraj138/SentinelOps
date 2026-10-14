| metric | naive-agent | naive-rule | sentinelops |
|---|---|---|---|
| runs | 17 | 17 | 17 |
| scored | 17 | 17 | 17 |
| not manifested | 0 | 0 | 0 |
| errors | 0 | 0 | 0 |
| correct team on first page | 100% (17/17) | 65% (11/17) | 100% (17/17) |
| answer passes the evidence check | 76% (13/17) | - | 100% (17/17) |
| citations: quote instead of a tool id | 21% (36/170) | - | 0% (0/202) |
| citations: no tool result behind them | 2% (3/170) | - | 0% (0/202) |
| root cause service | 100% (17/17) | 65% (11/17) | 100% (17/17) |
| correct action | 100% (17/17) | 41% (7/17) | 100% (17/17) |
| correct or partial | 100% (17/17) | 41% (7/17) | 100% (17/17) |
| harmful proposal | 0% (0/17) | 59% (10/17) | 0% (0/17) |
| escalation correct | 100% (17/17) | 76% (13/17) | 100% (17/17) |
| safe failure (escalated instead of a wrong fix) | no failures | 0% (0/10) | no failures |
| missed escalation (acted when only a human could fix it) | 0% (0/4) | 100% (4/4) | 0% (0/4) |
| unnecessary escalation (a typed fix existed) | 0% (0/13) | 0% (0/13) | 0% (0/13) |
| correct fix by stated confidence | high 16/16, medium 1/1 | low 7/17 | high 16/16, medium 1/1 |
| changes executed without approval | 76% (13/17) | - | - |
| wrong changes executed without approval | 0% (0/17) | - | - |
| healthy after its own change | 100% (13/13) | - | - |
| proposal could not be executed | 0% (0/17) | - | - |
| median time to answer | 27s | - | 28s |
| mean cost | $0.099 | - | $0.104 |
| mean tool calls | 9.6 | - | 10.6 |

| scenario (SentinelOps) | proposed-action grade per run |
|---|---|
| bad_deploy/a | full |
| bad_deploy/b | full |
| bad_deploy/c | full |
| bad_deploy/d | full |
| bad_deploy/e | full |
| bad_feature_flag/a | full |
| bad_feature_flag/b | full |
| db_connection_exhaustion/a | full |
| db_connection_exhaustion/b | full |
| db_outage/a | full |
| memory_leak/a | full |
| memory_leak/b | full |
| missing_secret/a | full |
| missing_secret/b | full |
| slow_dependency/a | full |
| slow_dependency/b | full |
| slow_dependency/c | full |
