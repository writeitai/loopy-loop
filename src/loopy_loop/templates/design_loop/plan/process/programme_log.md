# Programme log

Append-only record of every director decision. **Written only by the `design_director`
planner.** One entry per dispatch/conclusion decision:

```
## <n>. <date> — dispatched <workflow_set> | concluded | re-routed
Goal (summary): <one line>
Why now: <the judgment>
Evidence cited: <paths/IDs — child eval reports, phase reviews, register entries>
Outcome (filled on child completion): <accepted / needs-rework → entry n+1>
```

This log is what makes the director's freedom auditable: red-team rounds review it for
premature convergence (binding dispatched without sufficiency evidence) and aimless
exploration (repeated investigation with no option/ranking change).

<!-- No entries yet. -->
