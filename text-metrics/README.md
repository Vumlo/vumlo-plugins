# text-metrics

On `submission.submitted`, reads the submitted block document and posts a
report with word and character counts. It requests `submissions:read` and
`reports:write`. Course-scoped `minimum_words` and `maximum_words` settings are
declared for future threshold evaluation.

## Needs from core

The submitted-attempt API exposes content and IDs but not assignment settings
or a supported way to read the effective course setting in the event worker;
threshold values are therefore not applied until the plugin can read settings
with a course ID.
