"""`Plugin` end to end against `FakeCore`, with the example plugin of the README."""

import asyncio
import json

import httpx
import pytest

from tests.example_plugin import plugin
from vumlo_plugins import Plugin, State, clock, validate_manifest
from vumlo_plugins.testing import signed_call_headers, signed_event


@pytest.fixture
def world(fake):
    fake.attach(plugin)
    course = fake.add_course()
    teacher = fake.add_user("Tara", "Teacher", role="teacher")
    student = fake.add_user("Sam", "Student")
    fake.enroll(course, teacher, "teacher")
    fake.enroll(course, student)
    work = fake.add_assignment(course)
    return fake, course, teacher, student, work


async def test_the_manifest_is_served_and_valid(world):
    fake, *_ = world
    async with fake._plugin_http(plugin) as http:
        served = (await http.get("/manifest")).json()
    assert served == validate_manifest(served) == plugin.manifest()
    assert served["subscribes"] == ["submission.submitted", "plugin.settings.changed"]
    assert {(r["method"], r["path"]) for r in served["routes"]} == {
        ("GET", "/export/{course_id}/csv"), ("GET", "/whoami"), ("POST", "/jobs/{name}")}


async def test_an_event_is_answered_with_202_and_handled_afterwards(world):
    fake, course, teacher, student, work = world
    sub = fake.add_submission(work, student, text="one two three")
    response = await fake.deliver(plugin, "submission.submitted", fake.submission_event(sub), wait=False)
    assert response.status_code == 202
    await plugin.drain()
    report = fake.report("submission", sub, "word-count")
    assert report["summary"] == "3 words"
    assert report["data"]["display"] == {"level": "ok", "facts": [{"label": "Limit", "value": "500"}]}
    assert fake.mails == []


async def test_a_long_work_mails_the_student_once_even_if_the_event_comes_twice(world):
    fake, course, teacher, student, work = world
    fake.set_setting("max_words", 2, course)
    sub = fake.add_submission(work, student, text="one two three")
    await fake.deliver(plugin, "submission.submitted", fake.submission_event(sub), event_id="e1")
    await fake.deliver(plugin, "submission.submitted", fake.submission_event(sub), event_id="e1")  # redelivery
    await fake.deliver(plugin, "submission.submitted", fake.submission_event(sub), event_id="e2")  # another event
    assert fake.report("submission", sub, "word-count")["data"]["display"]["level"] == "warn"
    assert [m["kind"] for m in fake.mails_to(student)] == ["too_long"]  # the dedupe key held for e2
    assert sum(c.method == "PUT" for c in fake.calls) == 2  # e1 handled once, e2 once


async def test_a_failing_handler_is_logged_and_the_redelivery_is_processed_again(world, capsys):
    fake, course, teacher, student, work = world
    sub = fake.add_submission(work, student, text="a b")
    fake.fail("PUT", "/reports/", 422)  # a 422 is not retried: the handler raises
    await fake.deliver(plugin, "submission.submitted", fake.submission_event(sub), event_id="e1")
    assert fake.report("submission", sub, "word-count") is None
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failure = next(line for line in logged if line["msg"] == "event handler failed")
    assert failure["event_id"] == "e1" and "Invalid" in failure["error"]
    await fake.deliver(plugin, "submission.submitted", fake.submission_event(sub), event_id="e1")
    assert fake.report("submission", sub, "word-count")["summary"] == "2 words"


async def test_events_with_a_bad_signature_time_or_shape_are_refused(world):
    fake, *_ = world
    body, headers = signed_event(fake.secret, "submission.submitted", {})
    async with fake._plugin_http(plugin) as http:
        assert (await http.post("/events", content=body, headers={**headers, "x-vumlo-signature": "sha256=00"})).status_code == 401
        assert (await http.post("/events", content=body + b" ", headers=headers)).status_code == 401
        assert (await http.post("/events", content=body)).status_code == 401
        old, old_headers = signed_event(fake.secret, "submission.submitted", {}, timestamp=clock.timestamp() - 600)
        assert (await http.post("/events", content=old, headers=old_headers)).status_code == 401
        for junk in (b"[]", b'{"id": 1, "type": "x"}', b"not json", b'{"type": "x", "data": {}}'):
            stamp = str(clock.timestamp())
            from vumlo_plugins.signing import sign
            response = await http.post("/events", content=junk, headers={
                "x-vumlo-timestamp": stamp, "x-vumlo-signature": sign(fake.secret, stamp, junk)})
            assert response.status_code == 400, junk
    assert fake.calls == []


async def test_without_a_secret_events_and_routes_answer_503(fake):
    other = Plugin("other")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other.app), base_url="http://p") as http:
        body, headers = signed_event("x", "submission.submitted", {})
        assert (await http.post("/events", content=body, headers=headers)).status_code == 503
        health = (await http.get("/health")).json()
    assert health["configured"] is False and health["status"] == "ok"


async def test_health_says_nothing_secret(world):
    fake, *_ = world
    async with fake._plugin_http(plugin) as http:
        health = (await http.get("/health")).json()
    assert health["plugin"] == "word-count" and health["configured"] is True and health["jobs"] == ["sweep"]
    assert fake.secret not in json.dumps(health) and fake.token not in json.dumps(health)


async def test_a_setting_change_event_drops_the_cache(world):
    fake, course, teacher, student, work = world
    assert (await plugin.config.get(course["id"]))["max_words"] == 500
    fake.set_setting("max_words", 5, course)
    assert (await plugin.config.get(course["id"]))["max_words"] == 500  # cached
    await fake.deliver(plugin, "plugin.settings.changed", {"keys": ["max_words"]})
    assert (await plugin.config.get(course["id"]))["max_words"] == 5


async def test_a_route_serves_the_signed_identity_not_the_body(world):
    fake, course, teacher, student, work = world
    response = await fake.call(plugin, "GET", "/whoami", user=student)
    assert response.json() == {"user": student["id"], "role": "student"}
    assert (await fake.call(plugin, "GET", "/whoami")).status_code == 401  # the core wants a session
    # Straight to the plugin, without the core: no signature, no service.
    async with fake._plugin_http(plugin) as http:
        assert (await http.get("/whoami")).status_code == 401
        forged = {"x-vumlo-user": teacher["id"], "x-vumlo-role": "admin", "x-vumlo-timestamp": str(clock.timestamp()),
                  "x-vumlo-nonce": "n", "x-vumlo-signature": "sha256=" + "0" * 64}
        assert (await http.get("/whoami", headers=forged)).status_code == 401


async def test_a_signed_call_is_taken_once(world):
    fake, course, teacher, student, work = world
    headers = signed_call_headers(fake.secret, "GET", "/whoami", user=student["id"], role="student")
    async with fake._plugin_http(plugin) as http:
        assert (await http.get("/whoami", headers=headers)).status_code == 200
        assert (await http.get("/whoami", headers=headers)).status_code == 401


async def test_the_plugin_checks_roles_again_even_if_the_core_signed_them(world):
    fake, course, teacher, student, work = world
    headers = signed_call_headers(fake.secret, "GET", f"/export/{course['id']}/csv", user=student["id"],
                                  role="student", course=course["id"], course_role="student")
    async with fake._plugin_http(plugin) as http:
        assert (await http.get(f"/export/{course['id']}/csv", headers=headers)).status_code == 403


async def test_course_roles_gate_a_route_through_the_fake_core(world):
    fake, course, teacher, student, work = world
    other = fake.add_user("Out", "Sider", role="teacher")
    other_course = fake.add_course("Other")
    fake.enroll(other_course, other, "teacher")
    ok = await fake.call(plugin, "GET", f"/export/{course['id']}/csv", user=teacher)
    assert ok.status_code == 200 and ok.headers["content-disposition"].startswith("attachment")
    assert ok.text.split("\n") == [student["id"]]
    assert (await fake.call(plugin, "GET", f"/export/{course['id']}/csv", user=student)).status_code == 403
    assert (await fake.call(plugin, "GET", f"/export/{course['id']}/csv", user=other)).status_code == 404
    assert (await fake.call(plugin, "GET", f"/export/{course['id']}/csv", user=fake.admin)).status_code == 200


async def test_an_answer_the_core_would_not_pass_on_fails_the_test(world):
    fake, *_ = world
    local = Plugin("html")

    @local.route("GET", "/page", roles=["admin"])
    async def page(request, who):
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<h1>no</h1>")

    fake.attach(local)
    with pytest.raises(AssertionError, match="502"):
        await fake.call(local, "GET", "/page", user=fake.admin)
    assert (await fake.call(local, "GET", "/page", user=fake.admin, strict=False)).status_code == 200


def test_attachment_refuses_types_the_core_does_not_pass_on():
    from vumlo_plugins import attachment
    with pytest.raises(ValueError):
        attachment(b"x", "a.html", "text/html")
    assert attachment(b"x", "ünï code!.csv", "text/csv").headers["content-disposition"] == 'attachment; filename="uni_code_.csv"'


async def test_jobs_run_on_demand_by_an_administrator_only(world):
    fake, course, teacher, student, work = world
    assert await fake.run_job(plugin, "sweep") == {"job": "sweep", "ok": True, "swept": 0}
    assert (await fake.call(plugin, "POST", "/jobs/sweep", user=teacher)).status_code == 403
    assert (await fake.call(plugin, "POST", "/jobs/nope", user=fake.admin)).status_code == 404


async def test_a_failing_job_answers_500_without_leaking_the_message(fake):
    local = Plugin("jobs")

    @local.job("boom")
    async def boom():
        raise RuntimeError("secret detail of ada@example.com")

    fake.attach(local)
    response = await fake.call(local, "POST", "/jobs/boom", user=fake.admin, strict=False)
    assert response.status_code == 500 and response.json() == {"job": "boom", "ok": False, "error": "RuntimeError"}


async def test_the_lifespan_starts_and_stops_the_scheduler_and_rejects_a_bad_manifest(fake):
    local = Plugin("ticker")
    ticks = []

    @local.job("tick", every=0.05)
    async def tick():
        ticks.append(1)

    fake.attach(local)
    async with local.app.router.lifespan_context(local.app):
        await asyncio.sleep(0.3)
    assert ticks
    await asyncio.sleep(0.02)  # a run that was under way when it stopped may finish
    count = len(ticks)
    await asyncio.sleep(0.15)
    assert len(ticks) == count  # stopped
    bad = Plugin("Not Valid")
    with pytest.raises(Exception):
        async with bad.app.router.lifespan_context(bad.app):
            pass


async def test_the_example_plugin_state_remembers_events(world):
    fake, course, teacher, student, work = world
    assert isinstance(plugin.state, State)
    sub = fake.add_submission(work, student, text="x")
    await fake.deliver(plugin, "submission.submitted", fake.submission_event(sub), event_id="remember")
    assert "remember" in plugin.state.items("events")
