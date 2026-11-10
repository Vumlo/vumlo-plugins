"""Regression tests from the adversarial review of the SDK (stage S2): things that were broken, or could be."""

import asyncio
import json
import sqlite3
import threading

import httpx
import pytest

from vumlo_plugins import (CoreError, Plugin, PluginClient, RateLimited, Report, ServerError, State, Unreachable,
                           clock, logs, setting, validate_manifest)
from vumlo_plugins.manifest import ManifestError
from vumlo_plugins.signing import Refused, sign, verify_event, verify_proxy
from vumlo_plugins.testing import FakeCore, signed_call_headers, signed_event

SECRET = "s3cret-value"
UUID_A = "11111111-1111-4111-8111-111111111111"


def _plugin(**kwargs) -> Plugin:
    plugin = Plugin("probe", "1.0.0", **kwargs)

    @plugin.route("GET", "/whoami", roles=["admin", "teacher", "student"])
    async def whoami(request, who):
        return {"user": who.user_id, "role": who.role}

    @plugin.route("POST", "/echo", roles=["admin"])
    async def echo(request, who):
        return {"len": len(await request.body())}

    @plugin.route("GET", "/public/{token}", auth="none")
    async def public(request, who, token):
        return {"anonymous": who.anonymous, "user": who.user_id}

    plugin.configure(secret=SECRET, state=State(":memory:"))
    return plugin


def _http(plugin: Plugin) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=plugin.app), base_url="http://plugin.test")


# ---- signatures -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("junk", ["é", "sha256=é" + "0" * 60, "\xff" * 71])
def test_a_signature_with_non_ascii_bytes_is_refused_not_a_crash(junk):
    stamp = str(clock.timestamp())
    with pytest.raises(Refused) as caught:
        verify_event(SECRET, {"x-vumlo-timestamp": stamp, "x-vumlo-signature": junk}, b"{}")
    assert caught.value.status == 401
    headers = signed_call_headers(SECRET, "GET", "/whoami", user=UUID_A, role="admin")
    with pytest.raises(Refused) as caught:
        verify_proxy(SECRET, {**headers, "x-vumlo-signature": junk}, b"", method="GET", target="/whoami")
    assert caught.value.status == 401


@pytest.mark.parametrize("stamp", ["+{}", " {} ", "{}_0", "{}.0", "٠{}", "-{}", "0x{}", "{}\n"])
def test_a_timestamp_that_is_not_plain_digits_is_refused_even_if_signed(stamp):
    text = stamp.format(clock.timestamp())
    headers = {"x-vumlo-timestamp": text, "x-vumlo-signature": sign(SECRET, text, b"{}")}
    with pytest.raises(Refused) as caught:
        verify_event(SECRET, headers, b"{}")
    assert caught.value.status == 401


def test_the_edges_of_the_clock_window():
    now = clock.timestamp()
    for offset, ok in ((299, True), (300, True), (301, False), (-299, True), (-300, True), (-301, False)):
        body, headers = signed_event(SECRET, "x", {}, timestamp=now + offset)
        try:
            verify_event(SECRET, headers, body)
            assert ok, offset
        except Refused:
            assert not ok, offset


async def test_replay_with_another_method_target_or_body_is_refused():
    plugin = _plugin()
    async with _http(plugin) as http:
        headers = signed_call_headers(SECRET, "POST", "/echo", user=UUID_A, role="admin", body=b"abc")
        assert (await http.post("/echo", content=b"abd", headers=headers)).status_code == 401  # body changed
        headers = signed_call_headers(SECRET, "POST", "/echo", user=UUID_A, role="admin", body=b"abc")
        assert (await http.post("/echo", content=b"abc", headers=headers)).status_code == 200
        # A GET signed for /whoami cannot be sent as a POST to /echo, nor to /whoami with a query.
        headers = signed_call_headers(SECRET, "GET", "/whoami", user=UUID_A, role="admin")
        assert (await http.get("/whoami?x=1", headers=headers)).status_code == 401
        headers = signed_call_headers(SECRET, "GET", "/whoami", user=UUID_A, role="admin")
        assert (await http.post("/echo", headers=headers)).status_code == 401
        headers = signed_call_headers(SECRET, "GET", "/whoami", user=UUID_A, role="admin")
        assert (await http.get("/whoami", headers=headers)).status_code == 200


async def test_a_nonce_is_taken_once_even_when_the_same_call_arrives_at_the_same_time():
    plugin = _plugin()
    headers = signed_call_headers(SECRET, "GET", "/whoami", user=UUID_A, role="admin")
    async with _http(plugin) as http:
        answers = await asyncio.gather(*[http.get("/whoami", headers=headers) for _ in range(20)])
    assert sorted(a.status_code for a in answers).count(200) == 1


async def test_a_missing_or_empty_nonce_is_refused_even_with_a_matching_signature():
    plugin = _plugin()
    async with _http(plugin) as http:
        headers = signed_call_headers(SECRET, "GET", "/whoami", user=UUID_A, role="admin", nonce="x")
        del headers["x-vumlo-nonce"]
        assert (await http.get("/whoami", headers=headers)).status_code == 401
        headers = signed_call_headers(SECRET, "GET", "/whoami", user=UUID_A, role="admin", nonce="")
        assert (await http.get("/whoami", headers=headers)).status_code == 401


async def test_identity_headers_added_to_a_signed_call_never_change_who_it_is():
    plugin = _plugin()
    async with _http(plugin) as http:
        # A captured public call with a user and a role added: still nobody.
        headers = signed_call_headers(SECRET, "GET", "/public/t")
        forged = {**headers, "x-vumlo-user": UUID_A, "x-vumlo-role": "admin"}
        response = await http.get("/public/t", headers=forged)
        assert response.status_code == 200 and response.json() == {"anonymous": True, "user": None}
        # A captured session call turned into a public one: refused.
        headers = signed_call_headers(SECRET, "GET", "/whoami", user=UUID_A, role="student")
        assert (await http.get("/whoami", headers={**headers, "x-vumlo-anonymous": "1"})).status_code == 401
        # A captured student call with the role raised to admin: refused.
        headers = signed_call_headers(SECRET, "GET", "/whoami", user=UUID_A, role="student")
        assert (await http.get("/whoami", headers={**headers, "x-vumlo-role": "admin"})).status_code == 401


async def test_a_person_cannot_reach_a_public_route_and_nobody_reaches_a_session_route():
    plugin = _plugin()
    async with _http(plugin) as http:
        headers = signed_call_headers(SECRET, "GET", "/public/t", user=UUID_A, role="admin")
        assert (await http.get("/public/t", headers=headers)).status_code == 403
        headers = signed_call_headers(SECRET, "GET", "/whoami")
        assert (await http.get("/whoami", headers=headers)).status_code == 403


async def test_headers_in_any_case_are_taken_and_the_path_is_signed_as_sent():
    plugin = _plugin()
    async with _http(plugin) as http:
        headers = signed_call_headers(SECRET, "GET", "/public/a%20b", user=None)
        headers = {k.upper(): v for k, v in headers.items()}
        assert (await http.get("/public/a%20b", headers=headers)).status_code == 200


# ---- events -----------------------------------------------------------------------------------------------------


async def test_the_same_event_delivered_many_times_at_once_is_handled_once():
    plugin = _plugin()
    runs = []
    gate = asyncio.Event()

    @plugin.on("assignment.created")
    async def created(event):
        runs.append(event.id)
        await gate.wait()

    body, headers = signed_event(SECRET, "assignment.created", {}, event_id="e1")
    async with _http(plugin) as http:
        answers = await asyncio.gather(*[http.post("/events", content=body, headers=headers) for _ in range(25)])
        assert {a.status_code for a in answers} == {202}
        gate.set()
        await plugin.drain()
    assert runs == ["e1"]


async def test_an_event_of_a_type_nobody_listens_to_is_acknowledged_and_not_remembered():
    plugin = _plugin()
    body, headers = signed_event(SECRET, "grade.published", {}, event_id="e9")
    async with _http(plugin) as http:
        assert (await http.post("/events", content=body, headers=headers)).status_code == 202
    assert plugin.state.items("events") == {}


async def test_an_event_with_a_secret_it_was_not_signed_with_never_reaches_a_handler():
    plugin = _plugin()
    runs = []

    @plugin.on("assignment.created")
    async def created(event):
        runs.append(event)

    body, headers = signed_event("another-secret", "assignment.created", {})
    async with _http(plugin) as http:
        assert (await http.post("/events", content=body, headers=headers)).status_code == 401
    await plugin.drain()
    assert runs == [] and plugin.state.items("events") == {}


# ---- the scheduler ----------------------------------------------------------------------------------------------


async def test_a_job_started_twice_at_once_does_not_overlap():
    plugin = _plugin()
    active, peak = 0, 0

    @plugin.job("sweep")
    async def sweep():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"n": 1}

    results = await asyncio.gather(*[plugin.scheduler.run("sweep") for _ in range(5)])
    assert peak == 1 and all(r["ok"] for r in results)


async def test_a_job_result_cannot_overwrite_the_envelope_and_a_odd_result_is_tolerated():
    plugin = _plugin()

    @plugin.job("liar")
    async def liar():
        return {"ok": False, "job": "other", "n": 2}

    @plugin.job("odd")
    async def odd():
        return 42

    assert await plugin.scheduler.run("liar") == {"job": "liar", "ok": True, "n": 2}
    assert await plugin.scheduler.run("odd") == {"job": "odd", "ok": True}


def test_an_interval_must_be_positive():
    plugin = _plugin()
    for bad in (0, -5):
        with pytest.raises(ValueError):
            plugin.scheduler.add("x", lambda: None, every=bad)


# ---- State ------------------------------------------------------------------------------------------------------


def test_seen_is_true_for_exactly_one_of_many_threads_and_connections(tmp_path):
    states = [State(tmp_path), State(tmp_path)]  # two connections to one file: two workers
    wins, keys = [], [f"k{i}" for i in range(60)]

    def run(state):
        for key in keys:
            if state.seen("events", key):
                wins.append(key)

    threads = [threading.Thread(target=run, args=(states[i % 2],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(wins) == sorted(keys)


def test_a_corrupt_state_file_is_set_aside_and_a_fresh_one_starts(tmp_path):
    State(tmp_path).set("a", "b", 1)
    (tmp_path / "state.db").write_bytes(b"this is not an sqlite file" * 100)
    for leftover in ("state.db-wal", "state.db-shm"):
        (tmp_path / leftover).unlink(missing_ok=True)
    state = State(tmp_path)  # does not raise
    assert state.get("a", "b") is None
    state.set("a", "b", 2)
    assert State(tmp_path).get("a", "b") == 2
    assert any(p.name.startswith("state.db.corrupt") for p in tmp_path.iterdir())


def test_a_value_that_is_not_json_reads_as_missing(tmp_path):
    state = State(tmp_path)
    state.set("ns", "k", {"a": 1})
    raw = sqlite3.connect(tmp_path / "state.db")
    raw.execute("UPDATE kv SET value='{broken' WHERE ns='ns'")
    raw.commit()
    raw.close()
    assert state.get("ns", "k", default="gone") == "gone"
    assert state.items("ns") == {}


# ---- the client -------------------------------------------------------------------------------------------------


def _client(handler, **kwargs):
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    return PluginClient("http://core/api/v1", "vplg_" + "x" * 43, transport=httpx.MockTransport(handler),
                        sleep=sleep, **kwargs), slept


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
async def test_a_client_error_is_never_retried(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"detail": "no"})

    client, slept = _client(handler)
    with pytest.raises(CoreError):
        await client.request("GET", "/plugin-api/x")
    assert len(calls) == 1 and slept == []


@pytest.mark.parametrize("retry_after", ["nan", "-3", "abc", "Wed, 21 Oct 2026 07:28:00 GMT", ""])
async def test_a_retry_after_that_makes_no_sense_falls_back_to_a_back_off(retry_after):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": retry_after})
        return httpx.Response(200, json={"ok": True})

    client, slept = _client(handler)
    assert await client.request("GET", "/plugin-api/x") == {"ok": True}
    assert len(slept) == 1 and 0 <= slept[0] <= 60


async def test_retry_after_inf_is_not_slept_through():
    client, slept = _client(lambda r: httpx.Response(429, headers={"retry-after": "inf"}))
    with pytest.raises(RateLimited):
        await client.request("GET", "/plugin-api/x")
    assert slept == []


async def test_idempotent_writes_are_retried_after_a_5xx_but_a_post_is_not():
    for method, expected in (("PUT", 4), ("DELETE", 4), ("POST", 1)):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503, json={})

        client, _ = _client(handler)
        with pytest.raises(CoreError):
            await client.request(method, "/plugin-api/x", json={} if method != "DELETE" else None)
        assert len(calls) == expected, method


async def test_a_timeout_is_retried_for_a_read_then_reported_as_unreachable():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    client, slept = _client(handler)
    with pytest.raises(Unreachable):
        await client.request("GET", "/plugin-api/x")
    assert len(calls) == 4 and slept == [0.5, 1.0, 2.0]


async def test_a_post_is_resent_only_when_the_request_never_left():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(202, json={"accepted": 1, "skipped": 0, "skipped_reasons": {}})

    client, slept = _client(handler)
    assert (await client.mail.send(kind="k", subject="s", text="t", user_id=UUID_A)).accepted == 1
    assert len(calls) == 2
    calls.clear()

    def timeout(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)  # the core may have taken it

    client, _ = _client(timeout)
    with pytest.raises(Unreachable):
        await client.mail.send(kind="k", subject="s", text="t", user_id=UUID_A)
    assert len(calls) == 1


async def test_a_redirect_is_an_error_and_the_token_is_not_sent_elsewhere():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "http://evil.test/steal"})

    client, _ = _client(handler)
    with pytest.raises(CoreError) as caught:
        await client.request("GET", "/plugin-api/x")
    assert caught.value.status == 307 and seen == ["http://core/api/v1/plugin-api/x"]


async def test_an_answer_that_is_not_json_is_a_core_error_not_a_value_error():
    client, _ = _client(lambda r: httpx.Response(200, text="<html>proxy</html>", headers={"content-type": "text/html"}))
    with pytest.raises(ServerError):
        await client.request("GET", "/plugin-api/x")


async def test_without_an_api_address_the_client_fails_at_once_instead_of_retrying():
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    client = PluginClient("", "t", sleep=sleep)
    with pytest.raises(Unreachable) as caught:
        await client.request("GET", "/plugin-api/x")
    assert "VUMLO_API" in str(caught.value) and slept == []


async def test_a_cursor_loop_is_an_error_not_a_silent_cut():
    client, _ = _client(lambda r: httpx.Response(200, json={"items": [{"id": 1}], "next_cursor": "same"}))
    with pytest.raises(CoreError):
        await client.all("/plugin-api/x")


@pytest.mark.parametrize("bad", ["../../etc", "x/y", "1?x=2", "", "not-a-uuid", "11111111-1111-4111-8111-11111111111g"])
async def test_an_id_that_is_not_a_uuid_never_reaches_a_path(bad):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    client, _ = _client(handler)
    with pytest.raises(ValueError):
        await client.assignment(bad)
    with pytest.raises(ValueError):
        await client.reports.put("submission", bad, "k", Report("s"))
    with pytest.raises(ValueError):
        await client.members(bad)
    assert seen == []


async def test_an_upper_case_or_braced_uuid_is_normalised():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    client, _ = _client(handler)
    await client.assignment("{" + UUID_A.upper() + "}")
    assert seen == [f"/api/v1/plugin-api/assignments/{UUID_A}"]


@pytest.mark.parametrize("path", ["/\\evil.test", "/a/../b", "/a b", "/a\nb", "//evil.test", "evil"])
async def test_a_mail_link_stays_a_path_into_the_web_app(path):
    client, _ = _client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError):
        await client.mail.send(kind="k", subject="s", text="t", user_id=UUID_A, link_path=path)


async def test_a_mail_link_has_at_most_500_characters():
    client, _ = _client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError):
        await client.mail.send(kind="k", subject="s", text="t", user_id=UUID_A, link_path="/" + "a" * 500)


# ---- reports (the core counts bytes) -----------------------------------------------------------------------------


def test_a_summary_is_cut_to_500_bytes_because_the_core_counts_bytes():
    report = Report.build("Привіт, світе! " * 100)
    assert len(report.summary.encode()) <= 500 and report.summary.endswith("…")
    assert Report.build("x" * 500).summary == "x" * 500


async def test_the_fake_core_counts_a_summary_in_bytes_like_the_core(fake):
    course = fake.add_course()
    client = fake.client()
    ok = Report("я" * 250)  # 500 bytes
    await client.reports.put("course", course["id"], "k", ok)
    too_long = Report("я" * 251)
    with pytest.raises(ValueError):  # the client refuses before sending
        await client.reports.put("course", course["id"], "k", too_long)
    response = await client._http.put(
        f"http://core.test/api/v1/plugin-api/courses/{course['id']}/reports/k", json=too_long.body(),
        headers={"authorization": f"Bearer {fake.token}"})
    assert response.status_code == 422  # and so would the real one


# ---- logs ---------------------------------------------------------------------------------------------------------


def _lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


def test_nested_values_are_cleaned_by_the_name_of_their_key(capsys):
    log = logs.Log("probe-logs")
    log.info("x", request={"headers": {"Authorization": "Bearer abc", "x": [{"token": "zzz"}]}, "to": "a@b.org"})
    text = json.dumps(_lines(capsys))
    assert "abc" not in text and "zzz" not in text and "a@b.org" not in text


def test_the_secrets_of_the_environment_are_masked_wherever_they_appear(capsys, monkeypatch):
    monkeypatch.setenv("VUMLO_WEBHOOK_SECRET", "dGhpcy1pcy1hLXNlY3JldC12YWx1ZQ")
    monkeypatch.setenv("VUMLO_PLUGIN_TOKEN", "no-prefix-token-1234")
    log = logs.Log("probe-env")
    log.info("secret dGhpcy1pcy1hLXNlY3JldC12YWx1ZQ and no-prefix-token-1234", note="a=no-prefix-token-1234")
    try:
        raise RuntimeError("boom dGhpcy1pcy1hLXNlY3JldC12YWx1ZQ")
    except RuntimeError:
        log.exception("failed")
    text = capsys.readouterr().out
    assert "dGhpcy1" not in text and "no-prefix-token" not in text


def test_a_value_registered_as_secret_is_masked(capsys):
    log = logs.Log("probe-protect")
    log.protect("hunter2-hunter2")
    log.info("url https://hooks.test/hunter2-hunter2/x", n=3)
    assert "hunter2" not in capsys.readouterr().out


def test_addresses_tokens_and_credentials_in_urls_are_masked(capsys):
    log = logs.Log("probe-urls")
    log.warning("fetch https://user:pass@example.org/path failed for olena.s+x@uni.edu.ua with vplg_" + "A" * 43)
    text = capsys.readouterr().out
    assert "pass@" not in text and "olena" not in text and "vplg_" not in text


async def test_a_failing_handler_does_not_put_secrets_into_the_log(capsys):
    plugin = _plugin()
    token = "vplg_" + "Q" * 43

    @plugin.on("assignment.created")
    async def created(event):
        raise RuntimeError(f"core refused {token} for olena@uni.edu.ua with {SECRET}")

    plugin.log.protect(SECRET)
    body, headers = signed_event(SECRET, "assignment.created", {"email": "olena@uni.edu.ua"}, event_id="e1")
    async with _http(plugin) as http:
        await http.post("/events", content=body, headers=headers)
        await plugin.drain()
    text = capsys.readouterr().out
    assert "failed" in text
    assert token not in text and "olena" not in text and SECRET not in text


def test_the_image_does_not_log_requests_and_can_write_its_state():
    """uvicorn's access log prints paths, and a path can hold a token (the calendar feed); the state directory must
    belong to the user the image runs as."""
    from pathlib import Path
    docker = (Path(__file__).parent.parent / "Dockerfile").read_text()
    assert "--no-access-log" in docker
    assert "VUMLO_PLUGIN_DATA" in docker and "chown" in docker


# ---- settings -----------------------------------------------------------------------------------------------------


async def test_a_read_that_began_before_a_change_event_does_not_cache_the_old_value(fake):
    plugin = Plugin("slow", scopes=["settings:read"], settings={"n": setting("number", default=1)})
    fake.attach(plugin)
    fake.set_setting("n", 2)
    gate, started = asyncio.Event(), asyncio.Event()
    real = plugin.client.settings

    async def slow(course_id=None):
        values = await real(course_id)  # the old value is in hand ...
        started.set()
        await gate.wait()  # ... when the change event arrives
        return values

    plugin.client.settings = slow
    task = asyncio.create_task(plugin.config.get())
    await started.wait()
    fake.set_setting("n", 3)
    plugin.config.invalidate()
    gate.set()
    await task
    plugin.client.settings = real
    assert (await plugin.config.get())["n"] == 3


async def test_a_core_that_answers_oddly_gives_the_defaults(fake):
    plugin = Plugin("odd", scopes=["settings:read"], settings={"n": setting("number", default=1)})
    client = PluginClient("http://core/api/v1", "t", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    plugin.configure(client=client)
    assert await plugin.config.get() == {"n": 1}


# ---- the manifest validator versus manifest.rs -------------------------------------------------------------------


def test_a_method_is_upper_cased_like_ascii_only():
    base = {"id": "p", "version": "1", "api": "1"}
    assert validate_manifest({**base, "routes": [{"method": "get", "path": "/a", "roles": ["admin"]}]})["routes"][0]["method"] == "GET"
    # Rust `to_ascii_uppercase` leaves the long s alone, so `PoſT` is not POST there and must not be here.
    with pytest.raises(ManifestError):
        validate_manifest({**base, "routes": [{"method": "PoſT", "path": "/a", "roles": ["admin"]}]})


def test_minimum_and_maximum_must_be_finite():
    base = {"id": "p", "version": "1", "api": "1"}
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ManifestError):
            validate_manifest({**base, "settings": {"n": {"type": "number", "minimum": bad}}})
        with pytest.raises(ManifestError):
            validate_manifest({**base, "settings": {"n": {"type": "number", "maximum": bad}}})


# ---- FakeCore against the core ------------------------------------------------------------------------------------


async def test_the_fake_refuses_a_limit_or_cursor_outside_the_range_like_the_core(fake):
    """`PluginListQuery` validates `limit` 1..100 and `cursor` 1..100 characters with garde: a 422, not a clamp."""
    course = fake.add_course()
    for _ in range(3):
        fake.add_assignment(course)
    client = fake.client()
    for limit in (0, 101, 10_000):
        with pytest.raises(CoreError) as caught:
            await client.request("GET", "/plugin-api/assignments", params={"limit": limit})
        assert caught.value.status == 422, limit
    with pytest.raises(CoreError) as caught:
        await client.request("GET", "/plugin-api/assignments", params={"cursor": "x" * 101})
    assert caught.value.status == 422
    assert len((await client.request("GET", "/plugin-api/assignments", params={"limit": 2}))["items"]) == 2


async def test_the_fake_does_not_repair_a_plus_that_became_a_space_nor_a_time_without_an_offset(fake):
    client = fake.client()
    for bad in ("2026-03-02T12:00:00 01:00", "2026-03-02T12:00:00", "2026-03-02"):
        with pytest.raises(CoreError) as caught:
            await client.request("GET", "/plugin-api/assignments", params={"updated_since": bad})
        assert caught.value.status == 422, bad
    await client.request("GET", "/plugin-api/assignments", params={"updated_since": "2026-03-02T12:00:00+01:00"})


async def test_the_fake_answers_a_missing_route_to_a_stranger_with_401_like_the_core(fake):
    plugin = _plugin()
    fake.attach(plugin)
    assert (await fake.call(plugin, "GET", "/no/such/route")).status_code == 401
    assert (await fake.call(plugin, "GET", "/no/such/route", user=fake.admin)).status_code == 404


async def test_the_fake_fails_a_redirect_or_an_informational_answer_as_the_core_would(fake):
    plugin = Plugin("redir", scopes=[])

    @plugin.route("GET", "/go", roles=["admin"])
    async def go(request, who):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/elsewhere", status_code=302)

    fake.attach(plugin)
    with pytest.raises(AssertionError, match="502"):
        await fake.call(plugin, "GET", "/go", user=fake.admin)


async def test_the_fake_does_not_send_an_event_about_a_course_the_plugin_does_not_reach(fake):
    plugin = Plugin("ev", scopes=["assignments:read"])

    @plugin.on("assignment.created")
    async def created(event):
        pass

    fake.attach(plugin)
    mine, other = fake.add_course(), fake.add_course()
    fake.grant(courses=[mine])
    await fake.deliver(plugin, "assignment.created", {"course_id": mine["id"]})
    with pytest.raises(AssertionError, match="reach"):
        await fake.deliver(plugin, "assignment.created", {"course_id": other["id"]})


async def test_a_report_with_a_bad_body_is_422_even_for_a_subject_that_does_not_exist(fake):
    client = fake.client()
    with pytest.raises(CoreError) as caught:
        await client.request("PUT", f"/plugin-api/courses/{UUID_A}/reports/k", json={"summary": "", "data": {}})
    assert caught.value.status == 422  # extractors run before the handler looks the subject up
    with pytest.raises(CoreError) as caught:
        await client.request("PUT", f"/plugin-api/courses/{UUID_A}/reports/k", json={"summary": "s", "data": {}})
    assert caught.value.status == 404


# ---- second round: the request path, mail rules, the order of the core's answers -----------------------------------


async def test_a_sync_handler_that_fails_answers_502_and_the_redelivery_is_processed():
    plugin = _plugin()
    runs = []

    @plugin.on("assignment.created", sync=True)
    async def created(event):
        runs.append(event.id)
        if len(runs) == 1:
            raise RuntimeError("downstream is down")

    body, headers = signed_event(SECRET, "assignment.created", {}, event_id="e1")
    async with _http(plugin) as http:
        assert (await http.post("/events", content=body, headers=headers)).status_code == 502
        assert (await http.post("/events", content=body, headers=headers)).status_code == 202
        assert (await http.post("/events", content=body, headers=headers)).status_code == 202  # now a duplicate
    assert runs == ["e1", "e1"]


async def test_a_sync_handler_that_does_not_finish_in_time_answers_502(monkeypatch):
    from vumlo_plugins import plugin as module
    monkeypatch.setattr(module, "SYNC_TIMEOUT", 0.05)
    plugin = _plugin()

    @plugin.on("assignment.created", sync=True)
    async def created(event):
        await asyncio.sleep(5)

    body, headers = signed_event(SECRET, "assignment.created", {}, event_id="slow")
    async with _http(plugin) as http:
        assert (await http.post("/events", content=body, headers=headers)).status_code == 502
    assert plugin.state.items("events") == {}  # forgotten, so the core's next delivery runs


async def test_a_body_over_the_limit_is_refused_before_it_is_read_and_a_handler_can_read_it_again(monkeypatch):
    from vumlo_plugins import plugin as module
    monkeypatch.setattr(module, "MAX_ROUTE_BODY", 100)
    monkeypatch.setattr(module, "MAX_EVENT_BODY", 100)
    plugin = _plugin()
    async with _http(plugin) as http:
        big = b"x" * 101
        headers = signed_call_headers(SECRET, "POST", "/echo", user=UUID_A, role="admin", body=big)
        assert (await http.post("/echo", content=big, headers=headers)).status_code == 413
        async def chunked():  # no Content-Length: counted while it streams
            for _ in range(2):
                yield b"x" * 60

        assert (await http.post("/echo", content=chunked(), headers=headers)).status_code == 413
        body, headers = signed_event(SECRET, "x", {"pad": "y" * 200})
        assert (await http.post("/events", content=body, headers=headers)).status_code == 413
        small = b"x" * 100
        headers = signed_call_headers(SECRET, "POST", "/echo", user=UUID_A, role="admin", body=small)
        answer = await http.post("/echo", content=small, headers=headers)
        assert answer.status_code == 200 and answer.json() == {"len": 100}  # the handler read the body again


async def test_a_route_that_raises_is_logged_scrubbed_and_answers_500(capsys):
    plugin = Plugin("boom")
    token = "vplg_" + "R" * 43

    @plugin.route("GET", "/boom", roles=["admin"])
    async def boom(request, who):
        raise RuntimeError(f"cannot write for olena@uni.edu.ua with {token}")

    @plugin.route("GET", "/nope", roles=["admin"])
    async def nope(request, who):
        from fastapi import HTTPException
        raise HTTPException(404, "no such thing")

    @plugin.route("GET", "/when", roles=["admin"])
    async def when(request, who):
        import datetime
        return {"at": datetime.datetime(2026, 3, 2, 12, tzinfo=datetime.UTC)}

    plugin.configure(secret=SECRET, state=State(":memory:"))
    async with _http(plugin) as http:
        for path, status in (("/boom", 500), ("/nope", 404), ("/when", 200)):
            headers = signed_call_headers(SECRET, "GET", path, user=UUID_A, role="admin")
            assert (await http.get(path, headers=headers)).status_code == status, path
    text = capsys.readouterr().out
    assert "route failed" in text and "olena" not in text and token not in text


async def test_a_job_result_with_a_time_in_it_is_still_an_answer():
    import datetime
    plugin = _plugin()

    @plugin.job("when")
    async def when():
        return {"at": datetime.datetime(2026, 3, 2, 12, tzinfo=datetime.UTC)}

    async with _http(plugin) as http:
        headers = signed_call_headers(SECRET, "POST", "/jobs/when", user=UUID_A, role="admin")
        answer = await http.post("/jobs/when", headers=headers)
    assert answer.status_code == 200 and answer.json()["at"].startswith("2026-03-02")


def test_the_nonce_store_forgets_the_old_ones_from_the_front_and_refuses_when_full():
    from vumlo_plugins.signing import NonceStore
    store = NonceStore(limit=3)
    store.take("a", 0)
    store.take("b", 100)
    store.take("c", 200)
    with pytest.raises(Refused) as full:
        store.take("d", 300)
    assert full.value.status == 503
    store.take("d", 601)  # "a" is older than 600 s: forgotten, so there is room
    with pytest.raises(Refused) as seen:
        store.take("b", 601)  # "b" is still remembered
    assert seen.value.status == 401


def test_naive_times_are_utc_everywhere():
    from datetime import datetime
    assert clock.iso(datetime(2026, 3, 2, 12, 0, 0)) == "2026-03-02T12:00:00Z"


# -- mail rules of the core (mail crate model.rs)

@pytest.mark.parametrize("kwargs", [
    dict(text="a\x00b"), dict(text="a\x1bb"), dict(text="x" * 20001), dict(text=""),
    dict(dedupe_key="a\nb"), dict(dedupe_key="x" * 101),
    dict(link_path="/a//b"), dict(link_path="/a?b=c d"), dict(link_path="/" + "a" * 500),
])
async def test_a_mail_the_core_would_refuse_is_refused_here_and_in_the_fake(fake, kwargs):
    course = fake.add_course()
    student = fake.add_user()
    fake.enroll(course, student)
    base = dict(kind="k", subject="s", text="t", user_id=student["id"])
    with pytest.raises(ValueError):
        await fake.client().mail.send(**{**base, **kwargs})
    body = {"to": {"user_id": student["id"]}, "kind": "k", "subject": "s", "text": kwargs.get("text", "t")}
    body.update({k: v for k, v in kwargs.items() if k != "text"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app), base_url="http://core") as http:
        answer = await http.post("/api/v1/plugin-api/mail", json=body,
                                 headers={"authorization": f"Bearer {fake.token}"})
    assert answer.status_code == 422


async def test_line_breaks_and_tabs_are_fine_in_the_text(fake):
    course = fake.add_course()
    student = fake.add_user()
    fake.enroll(course, student)
    answer = await fake.client().mail.send(kind="k", subject="s", text="line 1\r\n\tline 2", user_id=student["id"])
    assert answer.accepted == 1


async def test_a_bad_body_is_422_before_the_scope_is_checked_and_a_bad_key_is_404_for_a_missing_subject(fake):
    """The core's extractors read the body before the handler asks for the scope or looks the subject up."""
    fake.grant(scopes={"settings:read"})
    client = fake.client()
    with pytest.raises(CoreError) as caught:
        await client.request("POST", "/plugin-api/mail", json={"to": 1})
    assert caught.value.status == 422
    with pytest.raises(CoreError) as caught:
        await client.request("PUT", f"/plugin-api/courses/{UUID_A}/reports/k", json={"summary": "", "data": {}})
    assert caught.value.status == 422
    with pytest.raises(CoreError) as caught:  # a valid body, but no scope
        await client.request("PUT", f"/plugin-api/courses/{UUID_A}/reports/k", json={"summary": "s", "data": {}})
    assert caught.value.status == 403
    fake.grant(scopes={"reports:write"})
    with pytest.raises(CoreError) as caught:  # the key is looked at after the subject
        await client.request("PUT", f"/plugin-api/courses/{UUID_A}/reports/Bad", json={"summary": "s", "data": {}})
    assert caught.value.status == 404
    course = fake.add_course()
    with pytest.raises(CoreError) as caught:
        await client.request("PUT", f"/plugin-api/courses/{course['id']}/reports/Bad", json={"summary": "s", "data": {}})
    assert caught.value.status == 422


async def test_a_course_outside_the_grant_is_403_even_when_it_does_not_exist_for_a_course_report(fake):
    fake.grant(courses=[fake.add_course()])
    with pytest.raises(CoreError) as caught:
        await fake.client().request("PUT", f"/plugin-api/courses/{UUID_A}/reports/k", json={"summary": "s", "data": {}})
    assert caught.value.status == 403  # `authorize_plugin_course` comes before `course_must_exist`


async def test_the_fake_checks_what_the_plugin_sends_against_the_openapi(fake):
    course = fake.add_course()
    with pytest.raises(AssertionError, match="request"):
        await fake.client().request("PUT", f"/plugin-api/courses/{course['id']}/reports/k",
                                    json={"summary": "s", "data": {}, "colour": "red"})


async def test_mail_hourly_limit_counts_calls_and_an_audience_over_the_limit_is_422(fake):
    course = fake.add_course()
    students = [fake.add_user() for _ in range(3)]
    for s in students:
        fake.enroll(course, s)
    fake.mail_limits.update(plugin_hourly_calls=2, max_audience=2)
    client = fake.client()
    with pytest.raises(CoreError) as caught:
        await client.mail.send(kind="k", course_id=course["id"], audience="students", subject="s", text="t")
    assert caught.value.status == 422 and fake.mails == []
    await client.mail.send(kind="k", course_id=course["id"], audience="teachers", subject="s", text="t")  # 0 people
    await client.mail.send(kind="k", user_id=students[0]["id"], subject="s", text="t")
    with pytest.raises(RateLimited) as limited:
        await client.mail.send(kind="k", user_id=students[1]["id"], subject="s", text="t")
    assert limited.value.retry_after == 3600


async def test_a_post_that_may_have_reached_the_core_is_sent_once():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadError("reset", request=request)

    client, _ = _client(handler)
    with pytest.raises(Unreachable):
        await client.mail.send(kind="k", subject="s", text="t", user_id=UUID_A)
    assert len(calls) == 1


async def test_when_the_core_fails_the_last_known_settings_beat_the_defaults(fake):
    plugin = Plugin("stale", scopes=["settings:read"], settings={"n": setting("number", default=1)})
    fake.attach(plugin)
    fake.set_setting("n", 7)
    assert (await plugin.config.get())["n"] == 7
    clock.advance(seconds=301)  # the cache has expired
    fake.fail("GET", "/plugin-api/settings", 503, times=50)
    assert (await plugin.config.get())["n"] == 7
    plugin.config.invalidate()  # after a change event nothing stale is served
    assert (await plugin.config.get())["n"] == 1


async def test_a_secret_setting_is_masked_in_the_log_once_it_was_read(fake, capsys):
    plugin = Plugin("hook", scopes=["settings:read"], settings={"url": setting("string", secret=True)})
    fake.attach(plugin)
    fake.set_setting("url", "https://hooks.example.org/T0K3N-value")
    assert (await plugin.config.get())["url"].startswith("https://")
    plugin.log.info("posting to https://hooks.example.org/T0K3N-value failed")
    assert "T0K3N" not in capsys.readouterr().out


def test_a_secret_with_a_trailing_newline_from_the_environment_still_verifies(monkeypatch):
    monkeypatch.setenv("VUMLO_WEBHOOK_SECRET", SECRET + "\n")
    monkeypatch.setenv("VUMLO_PLUGIN_TOKEN", "vplg_abc\n")
    plugin = Plugin("env")
    body, headers = signed_event(SECRET, "x", {})
    verify_event(plugin.secret, headers, body)
    assert PluginClient("http://core/api/v1")._token == "vplg_abc"
