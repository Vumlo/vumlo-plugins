# github-repo-check

Declares a secret GitHub token and the submission event/report scopes.

## Needs from core

The submitted content contract does not expose a form-assignment repository
field, assignment kind, or deadline. It therefore cannot decide which GitHub
repository to check or whether a commit preceded a deadline without guessing.
