# ungraded-sla-alert

Declares a grading SLA and receives submission/grade events.

## Needs from core

The plugin API exposes neither submission timestamps by assignment at list
scope nor grader/teacher recipients. A scheduler and grading-status query are
needed before SLA alerts can be correct.
