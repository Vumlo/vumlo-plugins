# vumlo_plugins: the SDK for plugin authors

A Vumlo plugin is a small HTTP service. The core calls it (signed events, signed routes), it calls the core back with its
token (`/plugin-api/*`). This package does the protocol so a plugin is only its own logic: manifest, signature checks,
a paginated retrying client, settings, reports, mail, jobs, state, logs.

Python 3.12, FastAPI, httpx. One folder per plugin with an `app.py` that exposes `app` (`uvicorn <folder>.app:app`).

## A plugin in 30 lines

<!-- example:start -->
```python
from vumlo_plugins import Plugin, setting

plugin = Plugin(
    "word-count", "1.0.0",
    scopes=["submissions:read", "reports:write", "settings:read", "mail:send"],
    settings={"max_words": setting("number", default=500, minimum=1, scope="course")},
)


@plugin.on("submission.submitted")  # runs after the core got its 202; must be idempotent
async def submitted(event):
    sub = await plugin.client.submission(event.data["submission_id"])
    words = sum(len(p["text"].split()) for b in sub["content"]["content"] for p in b["content"])
    limit = (await plugin.config.get(sub["course_id"]))["max_words"]
    report = plugin.report(f"{words} words", level="warn" if words > limit else "ok")
    await plugin.client.reports.put("submission", sub["id"], "word-count", report)  # same key: replaced
    if words > limit:
        await plugin.client.mail.send(kind="too_long", user_id=sub["student_id"], subject="Your work is long",
                                      text=f"{words} words, the limit is {limit}.", dedupe_key=sub["id"])


@plugin.route("GET", "/whoami", roles=["admin", "teacher", "student"])  # /api/v1/plugins/word-count/whoami
async def whoami(request, who):  # `who` is the signed identity, never read from the body
    return {"user": who.user_id, "role": who.role}


app = plugin.app  # uvicorn word-count.app:app
```
<!-- example:end -->

What you get without writing it:

- `GET /manifest` built from what you declared (events, scopes, settings, routes) and **checked by a port of the core's
  validator** when the server starts and in tests: a manifest the core would refuse at `plugin install` stops here.
- `POST /events`: verifies the signature and the age, records the event id, answers `202`, then runs your handler off the
  request path. A redelivery of the same id is ignored. The core has its `202` by then and sends nothing again, so a
  handler that raises is logged (scrubbed) and that event is lost: read what must not be lost again in a job. When the
  core must retry (a forwarder), declare `@plugin.on(type, sync=True)`: the handler runs before the answer, a failure or
  4 s without finishing answers `502`, the id is forgotten and the core's redelivery is processed.
- A route that raises is logged by the SDK (never by the server's own logger, which prints the raw message) and answers
  `500` with the exception type. Bodies above 10 MiB (1 MiB for events) are refused before they are read.
- Routes: every call is verified (signature over method, target, body, user, role, course, nonce; nonces are taken once)
  before your handler runs, and the signed roles are checked again. `auth="none"` is a public `GET` for which you check
  your own token.
- `GET /health` without secrets; `POST /jobs/{name}` (administrators) to run a scheduled job once, now.

Environment, printed by `vumlo-cli plugin install`: `VUMLO_API` (root with `/api/v1`), `VUMLO_PLUGIN_TOKEN`,
`VUMLO_WEBHOOK_SECRET`. Also `VUMLO_PLUGIN_DATA` (directory of the SQLite state, default `./data`) and `VUMLO_TZ`
(timezone of cron lines, default UTC).

## The pieces

| | |
|---|---|
| `plugin.client` | `assignments`, `assignment`, `submissions`, `submission`, `form_answers`, `members`, `grades`, `quiz`, `attempts`, `announcements`, `deleted_*`, `settings`; lists are followed through `next_cursor`; 429 is retried after `Retry-After`, 5xx with a back-off (never a `POST`); errors are typed (`Forbidden`, `NotFound`, `Gone`, `RateLimited`, `Unavailable`...), all `CoreError` |
| `client.reports.put(kind, id, key, report)` / `.delete` | `kind` is `submission`, `assignment` or `course`; `key` is a slug of at most 40 characters; the same key replaces, the core keeps 20 per subject; the summary is at most 500 **bytes** (the core counts bytes, so 250 Cyrillic letters); `delete` returns `False` when nothing was there |
| `Report.build(summary, level=, title=, columns=, rows=, facts=, links=)` | the display convention v1 the web "Plugin reports" panel renders; rows are cut to 200 and to 16 KiB and say so; `links` are paths on your own server, made into `/api/v1/plugins/<id>/...` |
| `client.mail.send(kind=, subject=, text=, user_id= or course_id= + audience=, link_path=, dedupe_key=)` | plain text; the plugin never sees an address; the same `dedupe_key` to the same person is accepted once in 30 days; the core adds the footer and the opt-out per `kind`; `skipped_reasons` has `opted_out`, `no_address`, `duplicate`, `disabled`, `rate_limited`; one person over the daily limit is a `429`, in an audience they are skipped |
| `plugin.config.get(course_id)` | settings with the defaults of the manifest, course over institution; cached, dropped on `plugin.settings.changed`; works with the core down |
| `@plugin.job(name, cron=, every=)` | time-based work (APScheduler, started with the app); must be idempotent: use dedupe keys and `plugin.state` |
| `plugin.state` | `State`: SQLite in WAL mode for caches and cursors, never the source of truth for core data |
| `clock.now()` | the only place a plugin reads the time; tests freeze it |
| `plugin.log` | JSON lines; fields called `token`, `email`... are never written; addresses and tokens in text are masked |
| `attachment(bytes, name, media_type)` | a download; the core passes on only JSON, text, CSV, iCalendar, PDF and XLSX |

Rules every plugin follows: take identity only from the signed headers; never log tokens, secrets or addresses; declare
only the scopes you use; write reports under stable keys; mail only text.

## Testing with FakeCore

`vumlo_plugins.testing.FakeCore` is the core's `/plugin-api/*` in process, with its scopes, course grants, pagination,
tombstones (`410`), report limits and mail rules (limits, dedupe, audiences, opt-out, `403`/`404`/`429`/`503`). It also
signs events and proxied calls exactly like the core. No network, no clock to wait for.

```python
import importlib

from vumlo_plugins import clock
from vumlo_plugins.testing import FakeCore

# A folder name has dashes, so import by string; `app.state.plugin` is the Plugin behind `app`.
plugin = importlib.import_module("word-count.app").app.state.plugin

async def test_a_long_work_is_reported_and_mailed():
    clock.freeze("2026-03-02T12:00:00Z")
    fake = FakeCore()
    fake.attach(plugin)                                     # client, state, secret and scopes of the plugin
    course = fake.add_course()
    student = fake.add_user("Sam", "Student"); fake.enroll(course, student)
    work = fake.add_assignment(course)
    fake.set_setting("max_words", 2, course)
    sub = fake.add_submission(work, student, text="one two three")

    await fake.deliver(plugin, "submission.submitted", fake.submission_event(sub))   # signed, then waits for the work
    assert fake.report("submission", sub, "word-count")["summary"] == "3 words"
    assert [m["kind"] for m in fake.mails_to(student)] == ["too_long"]

    response = await fake.call(plugin, "GET", "/whoami", user=student)     # a person through the core
    assert response.json()["role"] == "student"
    # With an @plugin.job("sweep", ...): an administrator's POST /jobs/sweep, no waiting for the clock.
    # await fake.run_job(plugin, "sweep")
```

Useful knobs: `fake.grant(scopes=..., courses=[...])` (what the administrator granted), `fake.fail("GET", "/assignments", 503,
times=2, retry_after=1)` (retries), `fake.mail_limits` (`plugin_hourly_calls`, `recipient_daily`, `max_audience`, the names of `[mail]`), `fake.mail_configured = False`, `fake.opt_out(user, kind)`,
`clock.advance(days=1)`, `fake.calls` (what the plugin called), `fake.slept` (back-off waits, not really slept).

`fake.call` does what the core does for a route: 401 without a session (also for a route that does not exist), 404 for an
undeclared route or a course the person cannot see, 403 for a role, a course role or a course outside the grant, and it
fails your test where the core would answer `502` (a 5xx, a redirect, or a content type it does not pass on).
`fake.deliver` fails your test for an event the core would not send: one the plugin did not subscribe to, or one about a
course outside the grant.

### The fake is checked against the core

Every request and answer of `FakeCore` is validated against the core's generated OpenAPI
(`vumlo_plugins/testing/openapi_plugin_api.json`), and `tests/test_fake_core_contract.py` fails when the fake serves a
shape the core does not, or lacks a route the core documents. When the core's plugin API changes:

```sh
uv run python tools/refresh_openapi.py        # reads ../vumlo-core/web/openapi.json
uv run pytest
```

The mail endpoint and its schemas are written in `testing/contract.py` from the contract until the core's export carries them.

## Checking a manifest

```python
from vumlo_plugins import validate_manifest
validate_manifest(plugin.manifest())          # raises ManifestError with the core's own words
```

`tests/test_manifest.py` imports every `<folder>/app.py` of the repository and validates its manifest, so adding a
plugin adds the test. The validator is a port of `manifest.rs` and `settings.rs`; when the core changes a rule, change
`vumlo_plugins/manifest.py` and the test that mirrors it.

## Signatures

Defined by the core (`secret.rs`, `proxy.rs`); `tests/test_signing.py` holds the vectors the core's own tests assert.

- Event: `X-Vumlo-Signature: sha256=` + hex HMAC-SHA256(secret, `timestamp "." body`), `X-Vumlo-Timestamp` within 300 s.
- Proxied route: the same MAC over `timestamp "." canonical`, canonical being these lines joined by `\n`: `vumlo-proxy`,
  nonce, METHOD, request-target (path and query), `session` or `none`, user, role, course, course role, hex SHA-256 of
  the body. `-` stands for what is missing. The headers `X-Vumlo-User/-Role/-Course/-Course-Role` only repeat what the
  signature covers.
