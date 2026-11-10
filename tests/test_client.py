"""`PluginClient` against `FakeCore`: reads, pagination, retries, typed errors, reports, mail."""

from datetime import timedelta

import httpx
import pytest

from vumlo_plugins import (CoreError, Forbidden, Gone, Invalid, NotFound, PluginClient, RateLimited, Report,
                           ServerError, Unauthorized, Unavailable, Unreachable, clock)
from vumlo_plugins.errors import error_for


@pytest.fixture
def world(fake):
    course = fake.add_course("Algebra")
    teacher = fake.add_user("Tara", "Teacher", role="teacher")
    fake.enroll(course, teacher, "teacher")
    students = [fake.add_user(f"S{i}", "Student") for i in range(5)]
    for s in students:
        fake.enroll(course, s)
    work = fake.add_assignment(course, title="Essay", due_at="2026-03-10T09:00:00Z")
    return fake, fake.client(), course, teacher, students, work


async def test_reads(world):
    fake, client, course, teacher, students, work = world
    sub = fake.add_submission(work, students[0], text="hello world")
    fake.publish_grade(work, students[0], 80)
    assert [a["id"] for a in await client.assignments(course["id"])] == [work["id"]]
    assert (await client.assignment(work["id"]))["title"] == "Essay"
    assert (await client.submission(sub["id"]))["student_id"] == students[0]["id"]
    assert [s["id"] for s in await client.submissions(work["id"])] == [sub["id"]]
    assert len(await client.members(course["id"])) == 6
    assert [m["user_id"] for m in await client.members(course["id"], "teacher")] == [teacher["id"]]
    assert (await client.grades(course["id"]))[0]["score"] == "80"
    assert await client.attempts(work["id"]) == []
    assert await client.announcements(course["id"]) == []
    await client.aclose()


async def test_pagination_follows_the_cursor_to_the_end(world):
    fake, client, course, *_ = world
    for _ in range(130):
        fake.add_assignment(course)
    items = await client.assignments(course["id"])
    assert len(items) == 131 and len({i["id"] for i in items}) == 131
    pages = [c for c in fake.calls if c.template == "/plugin-api/assignments"]
    assert len(pages) == 3 and all("limit" not in c.query for c in pages[:1])


async def test_a_cursor_that_repeats_is_an_error_not_a_silently_cut_list(fake):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("cursor"))
        return httpx.Response(200, json={"items": [{"id": len(seen)}], "next_cursor": "same"})

    client = PluginClient("http://core/api/v1", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(CoreError, match="repeats"):
        await client.all("/plugin-api/x")
    assert seen == [None, "same"]


async def test_updated_since_takes_datetimes_and_strings(world):
    fake, client, course, *_ = world
    old = fake.add_assignment(course, title="Old", updated_at="2026-01-01T00:00:00Z")
    since = clock.now() - timedelta(days=1)
    assert [a["title"] for a in await client.assignments(course["id"], updated_since=since)] == ["Essay"]
    assert len(await client.assignments(course["id"], updated_since="2025-12-31T00:00:00Z")) == 2
    assert old["title"] == "Old"


async def test_deleted_since_and_gone(world):
    fake, client, course, *_ = world
    extra = fake.add_assignment(course)
    fake.delete_assignment(extra)
    gone = await client.deleted_assignments(clock.now() - timedelta(days=1), course["id"])
    assert [g["id"] for g in gone] == [extra["id"]]
    with pytest.raises(Gone):
        await client.deleted_assignments(clock.now() - timedelta(days=45))


async def test_quiz_is_served_without_its_key(world):
    fake, client, course, teacher, students, work = world
    fake.add_quiz(work, {"questions": [{"id": "q1", "type": "single", "points": 1, "options": [
        {"id": "a", "text": "A", "correct": True}, {"id": "b", "text": "B"}], "explanation": "because"}]})
    quiz = await client.quiz(work["id"])
    assert quiz["config"]["questions"][0]["options"][0] == {"id": "a", "text": "A"}
    assert "explanation" not in quiz["config"]["questions"][0]


async def test_settings_default_to_the_manifest_and_follow_course_over_institution(world):
    fake, client, course, *_ = world
    fake.setting_specs = {"a": {"type": "number", "default": 1, "scope": "course"}, "b": {"type": "string"}}
    assert await client.settings() == {"a": 1}
    fake.set_setting("a", 2, course)
    assert await client.settings(course["id"]) == {"a": 2}
    assert await client.settings() == {"a": 1}


# ---- errors ------------------------------------------------------------------------------------------------------


async def test_status_codes_become_typed_errors(world):
    fake, client, course, teacher, students, work = world
    with pytest.raises(NotFound) as missing:
        await client.assignment("00000000-0000-4000-8000-0000000fffff")
    assert missing.value.status == 404 and "no such assignment" in str(missing.value)
    with pytest.raises(ValueError):  # the client does not send an id that is not a UUID ...
        await client.assignment("not-a-uuid")
    with pytest.raises(Invalid) as malformed:  # ... and the core would answer 400 (axum's path rejection)
        await client.request("GET", "/plugin-api/assignments/not-a-uuid")
    assert malformed.value.status == 400
    fake.grant(scopes={"reports:write"})
    with pytest.raises(Forbidden):
        await client.assignments()
    bad = fake.client()
    bad._token = "wrong"
    with pytest.raises(Unauthorized):
        await bad.assignments()
    assert all(issubclass(e, CoreError) for e in (Forbidden, NotFound, Gone, Invalid, RateLimited, Unavailable,
                                                  ServerError, Unreachable, Unauthorized))


async def test_courses_outside_the_grant_are_forbidden_and_left_out_of_lists(world):
    fake, client, course, *_ = world
    other = fake.add_course("Other")
    fake.add_assignment(other)
    fake.grant(courses=[course])
    assert len(await client.assignments()) == 1
    with pytest.raises(Forbidden):
        await client.assignments(other["id"])
    with pytest.raises(Forbidden):
        await client.members(other["id"])


def test_error_for_maps_every_status():
    assert type(error_for(503, {}, None)) is Unavailable
    assert type(error_for(502, {}, None)) is ServerError
    assert type(error_for(418, {}, None)) is CoreError
    assert error_for(429, {"detail": "slow down"}, 5.0).retry_after == 5.0
    assert "slow down" in str(error_for(429, {"detail": "slow down"}, 5.0))


async def test_a_429_is_retried_after_its_retry_after_then_succeeds(world):
    fake, client, course, *_ = world
    fake.fail("GET", "/plugin-api/assignments", 429, times=2, retry_after=3)
    assert len(await client.assignments(course["id"])) == 1
    assert fake.slept == [3.0, 3.0]


async def test_a_5xx_is_retried_with_a_back_off_and_then_raises(world):
    fake, client, course, *_ = world
    fake.fail("GET", "/plugin-api/settings", 502, times=2)
    assert await client.settings() == {}
    assert fake.slept == [0.5, 1.0]
    fake.slept.clear()
    fake.fail("GET", "/plugin-api/settings", 500, times=10)
    with pytest.raises(ServerError):
        await client.settings()
    assert fake.slept == [0.5, 1.0, 2.0]  # three retries, then the error


async def test_a_wait_longer_than_a_minute_is_not_slept_through(world):
    fake, client, course, *_ = world
    fake.fail("GET", "/plugin-api/settings", 429, times=1, retry_after=3600)
    with pytest.raises(RateLimited) as limited:
        await client.settings()
    assert limited.value.retry_after == 3600 and fake.slept == []


async def test_a_lost_connection_is_retried_for_reads_but_a_post_only_when_it_never_left():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.method)
        raise httpx.ConnectError("down")  # nothing reached the core: even a POST may go again

    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    client = PluginClient("http://core/api/v1", "t", transport=httpx.MockTransport(handler), sleep=sleep)
    with pytest.raises(Unreachable) as down:
        await client.assignments()
    assert attempts == ["GET"] * 4 and slept == [0.5, 1.0, 2.0] and down.value.status == 0
    attempts.clear()
    with pytest.raises(Unreachable):
        await client.mail.send(kind="k", subject="s", text="t", user_id="00000000-0000-4000-8000-000000000001")
    assert attempts == ["POST"] * 4  # (a read timeout of a POST is sent once: see test_adversarial)


async def test_a_mail_5xx_is_not_retried_but_a_429_is(world):
    fake, client, course, teacher, students, work = world
    fake.fail("POST", "/plugin-api/mail", 502)
    with pytest.raises(ServerError):
        await client.mail.send(kind="k", subject="s", text="t", user_id=students[0]["id"])
    assert fake.slept == [] and fake.mails == []
    fake.fail("POST", "/plugin-api/mail", 429, retry_after=1)
    result = await client.mail.send(kind="k", subject="s", text="t", user_id=students[0]["id"])
    assert result.accepted == 1 and fake.slept == [1.0]


async def test_a_token_never_appears_in_an_error(world):
    fake, client, *_ = world
    client._token = "vplg_" + "S" * 43
    with pytest.raises(Unauthorized) as error:
        await client.assignments()
    assert "vplg_" not in str(error.value) and "vplg_" not in repr(error.value.problem)


# ---- reports -----------------------------------------------------------------------------------------------------


async def test_a_report_is_replaced_not_piled_up_and_can_be_deleted(world):
    fake, client, course, teacher, students, work = world
    sub = fake.add_submission(work, students[0], text="x")
    for words in ("1 word", "2 words"):
        await client.reports.put("submission", sub["id"], "text-metrics", Report.build(words, level="ok"))
    assert list(fake.reports_for("submission", sub)) == ["text-metrics"]
    assert fake.report("submission", sub, "text-metrics")["summary"] == "2 words"
    await client.reports.put("assignment", work["id"], "a", Report.build("x"))
    await client.reports.put("course", course["id"], "c", Report.build("x"))
    await client.reports.delete("submission", sub["id"], "text-metrics")
    assert fake.report("submission", sub, "text-metrics") is None
    with pytest.raises(NotFound):
        await client.reports.delete("submission", sub["id"], "text-metrics")


async def test_report_arguments_are_checked_before_anything_is_sent(world):
    fake, client, course, teacher, students, work = world
    for key in ("", "Upper", "1x", "x" * 41, "a/b", "a b"):
        with pytest.raises(ValueError):
            await client.reports.put("course", course["id"], key, Report.build("x"))
    with pytest.raises(ValueError):
        await client.reports.put("grade", course["id"], "k", Report.build("x"))
    with pytest.raises(ValueError):
        await client.reports.put("course", course["id"], "k", Report("", {}))
    with pytest.raises(ValueError):
        await client.reports.put("course", course["id"], "k", Report("x", {"big": "z" * 20000}))
    assert fake.calls == []


async def test_the_core_keeps_at_most_twenty_reports_per_subject(world):
    fake, client, course, *_ = world
    for i in range(20):
        await client.reports.put("course", course["id"], f"k{i}", Report.build("x"))
    with pytest.raises(Invalid) as many:
        await client.reports.put("course", course["id"], "k20", Report.build("x"))
    assert "at most 20" in str(many.value)
    await client.reports.put("course", course["id"], "k3", Report.build("replaced"))  # replacing is fine


async def test_a_report_on_a_subject_outside_the_grant_is_forbidden(world):
    fake, client, course, *_ = world
    fake.grant(courses=[])
    with pytest.raises(Forbidden):
        await client.reports.put("course", course["id"], "k", Report.build("x"))


# ---- mail (Contract 1) -------------------------------------------------------------------------------------------


async def test_mail_to_one_person_and_to_an_audience(world):
    fake, client, course, teacher, students, work = world
    one = await client.mail.send(kind="deadline_changed", user_id=students[0]["id"], subject="Moved",
                                 text="New deadline", link_path=f"/courses/{course['id']}/assignments/{work['id']}",
                                 dedupe_key="d1")
    assert (one.accepted, one.skipped, one.skipped_reasons) == (
        1, 0, {"opted_out": 0, "no_address": 0, "duplicate": 0, "disabled": 0, "rate_limited": 0})
    everyone = await client.mail.send(kind="deadline_changed", course_id=course["id"], audience="students",
                                      subject="Moved", text="New deadline", dedupe_key="d1")
    assert (everyone.accepted, everyone.skipped_reasons["duplicate"]) == (4, 1)  # the first student had it already
    teachers = await client.mail.send(kind="digest", course_id=course["id"], audience="teachers", subject="s", text="t")
    assert teachers.accepted == 1 and fake.mails_to(teacher)[0]["kind"] == "digest"
    everybody = await client.mail.send(kind="all_hands", course_id=course["id"], audience="all", subject="s", text="t")
    assert everybody.accepted == 6
    assert fake.mails_to(students[0])[0]["link_path"] == f"/courses/{course['id']}/assignments/{work['id']}"


async def test_mail_skips_the_opted_out_the_disabled_and_those_without_an_address(world):
    fake, client, course, teacher, students, work = world
    fake.opt_out(students[0], "news")
    fake.users[students[1]["id"]]["has_address"] = False
    fake.users[students[2]["id"]]["status"] = "blocked"
    result = await client.mail.send(kind="news", course_id=course["id"], audience="students", subject="s", text="t")
    assert (result.accepted, result.skipped) == (2, 3)
    assert result.skipped_reasons == {"opted_out": 1, "no_address": 1, "duplicate": 0, "disabled": 1,
                                      "rate_limited": 0}
    other_kind = await client.mail.send(kind="other", user_id=students[0]["id"], subject="s", text="t")
    assert other_kind.accepted == 1  # an opt-out is per kind


async def test_the_dedupe_key_holds_for_thirty_days_per_recipient(world):
    fake, client, course, teacher, students, work = world
    send = lambda key, s=students[0]: client.mail.send(kind="k", user_id=s["id"], subject="s", text="t", dedupe_key=key)
    assert (await send("a")).accepted == 1
    assert (await send("a")).skipped_reasons["duplicate"] == 1
    assert (await send("a", students[1])).accepted == 1  # another recipient
    assert (await send("b")).accepted == 1  # another key
    clock.advance(days=29)
    assert (await send("a")).accepted == 0
    clock.advance(days=2)
    assert (await send("a")).accepted == 1
    plain = lambda: client.mail.send(kind="k", user_id=students[3]["id"], subject="s", text="t")
    assert (await plain()).accepted == 1 and (await plain()).accepted == 1  # no key, no dedupe


async def test_mail_403_404_and_scope(world):
    fake, client, course, teacher, students, work = world
    stranger = fake.add_user("No", "Roster")
    with pytest.raises(Forbidden):  # on no roster the plugin reaches
        await client.mail.send(kind="k", user_id=stranger["id"], subject="s", text="t")
    with pytest.raises(NotFound):
        await client.mail.send(kind="k", user_id="00000000-0000-4000-8000-0000000fffff", subject="s", text="t")
    other = fake.add_course("Other")
    fake.enroll(other, stranger)
    fake.grant(courses=[course])
    with pytest.raises(Forbidden):
        await client.mail.send(kind="k", user_id=stranger["id"], subject="s", text="t")
    with pytest.raises(Forbidden):
        await client.mail.send(kind="k", course_id=other["id"], audience="all", subject="s", text="t")
    with pytest.raises(NotFound):
        await client.mail.send(kind="k", course_id="00000000-0000-4000-8000-0000000fffff", audience="all",
                               subject="s", text="t")
    fake.grant(scopes={"reports:write"}, courses=None)
    with pytest.raises(Forbidden) as denied:
        await client.mail.send(kind="k", user_id=students[0]["id"], subject="s", text="t")
    assert "mail:send" in str(denied.value)


async def test_mail_without_configuration_is_503(world):
    fake, client, course, teacher, students, work = world
    fake.mail_configured = False
    with pytest.raises(Unavailable) as off:
        await client.mail.send(kind="k", user_id=students[0]["id"], subject="s", text="t")
    assert off.value.status == 503 and fake.slept == [] and fake.mails == []


async def test_mail_limits_answer_429_with_retry_after(world):
    fake, client, course, teacher, students, work = world
    fake.mail_limits["max_audience"] = 3
    with pytest.raises(Invalid) as large:  # not a 429: asking again changes nothing
        await client.mail.send(kind="k", course_id=course["id"], audience="students", subject="s", text="t")
    assert large.value.status == 422 and fake.mails == []
    fake.mail_limits.update(max_audience=2000, recipient_daily=2)
    for _ in range(2):
        await client.mail.send(kind="k", user_id=students[0]["id"], subject="s", text="t")
    with pytest.raises(RateLimited) as day:
        await client.mail.send(kind="k", user_id=students[0]["id"], subject="s", text="t")
    assert 0 < day.value.retry_after <= 86400
    # An audience skips the person who is over the limit and mails the rest.
    answer = await client.mail.send(kind="k", course_id=course["id"], audience="students", subject="s", text="t")
    assert (answer.accepted, answer.skipped_reasons["rate_limited"]) == (4, 1)
    clock.advance(days=1, seconds=1)
    await client.mail.send(kind="k", user_id=students[0]["id"], subject="s", text="t")
    fake.mail_limits.update(plugin_hourly_calls=2, recipient_daily=20)
    clock.advance(hours=2)
    await client.mail.send(kind="k", user_id=students[1]["id"], subject="s", text="t")
    await client.mail.send(kind="k", user_id=students[2]["id"], subject="s", text="t")
    with pytest.raises(RateLimited):
        await client.mail.send(kind="k", user_id=students[3]["id"], subject="s", text="t")
    clock.advance(hours=1, seconds=1)
    await client.mail.send(kind="k", user_id=students[3]["id"], subject="s", text="t")


@pytest.mark.parametrize("kwargs", [
    dict(),  # nobody to send to
    dict(user_id="u", course_id="c", audience="all"),
    dict(course_id="c"),
    dict(course_id="c", audience="parents"),
    dict(user_id="u", audience="all"),
    dict(user_id="u", kind="Bad Kind"),
    dict(user_id="u", kind="k" * 41),
    dict(user_id="u", kind="1k"),
    dict(user_id="u", subject=""),
    dict(user_id="u", subject="s" * 201),
    dict(user_id="u", subject="line\nbreak"),
    dict(user_id="u", text=""),
    dict(user_id="u", text="t" * 20001),
    dict(user_id="u", link_path="courses/1"),
    dict(user_id="u", link_path="//evil.example/x"),
    dict(user_id="u", dedupe_key=""),
    dict(user_id="u", dedupe_key="d" * 101),
])
async def test_mail_arguments_are_checked_before_anything_is_sent(world, kwargs):
    fake, client, *_ = world
    base = dict(kind="k", subject="s", text="t")
    with pytest.raises(ValueError):
        await client.mail.send(**{**base, **kwargs})
    assert fake.calls == []


async def test_the_fake_enforces_the_same_rules_when_called_around_the_client(world):
    fake, client, course, teacher, students, work = world
    headers = {"Authorization": f"Bearer {fake.token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app), base_url="http://core") as http:
        for body in ({"kind": "k", "subject": "s", "text": "t"},
                     {"to": {"user_id": students[0]["id"]}, "kind": "k", "subject": "s", "text": "t", "extra": 1},
                     {"to": {"user_id": students[0]["id"], "email": "a@b.c"}, "kind": "k", "subject": "s", "text": "t"},
                     {"to": {"user_id": "nope"}, "kind": "k", "subject": "s", "text": "t"}):
            response = await http.post("/api/v1/plugin-api/mail", json=body, headers=headers)
            assert response.status_code == 422, body
        assert (await http.post("/api/v1/plugin-api/mail", json={})).status_code == 401
