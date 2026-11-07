# text-metrics

On `submission.submitted`, reads the submitted block document and posts a
report with word and character counts plus course-scoped min/max checks. It
requests `submissions:read`, `reports:write`, and `settings:read`.

## Needs from core

None. `GET /plugin-api/settings?course_id=...` exists and the plugin now calls
it with the event's `course_id`.
