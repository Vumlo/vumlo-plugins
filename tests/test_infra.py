"""State, settings reader, clock, logs, scheduler, and the helpers of FakeCore."""

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from vumlo_plugins import Plugin, State, clock, setting
from vumlo_plugins.logs import Log, scrub
from vumlo_plugins.scheduler import Scheduler
from vumlo_plugins.settings import SettingsReader

# ---- clock -------------------------------------------------------------------------------------------------------


def test_the_clock_is_frozen_by_the_fixture_and_can_move():
    assert clock.iso(clock.now()) == "2026-03-02T12:00:00Z"
    assert clock.advance(hours=2).hour == 14
    clock.unfreeze()
    with pytest.raises(RuntimeError):
        clock.advance(hours=1)
    assert abs((clock.now() - datetime.now(UTC)).total_seconds()) < 5  # unfrozen is the real time
    with clock.frozen("2020-01-01T00:00:00Z"):
        assert clock.timestamp() == 1_577_836_800
    assert clock.now().year >= 2026


def test_time_parsing_and_formatting_round_trip_the_cores_forms():
    for text in ("2026-03-01T10:00:00Z", "2026-03-01T10:00:00.123456Z", "2026-03-01T12:00:00+02:00"):
        assert clock.parse_time(text).utcoffset() is not None
    assert clock.parse_time("2026-03-01T12:00:00+02:00") == clock.parse_time("2026-03-01T10:00:00Z")
    assert clock.parse_time("2026-03-01T10:00:00").tzinfo is not None
    assert clock.iso(datetime(2026, 3, 1, 12, tzinfo=timezone_plus2())) == "2026-03-01T10:00:00Z"
    assert clock.freeze(datetime(2026, 1, 1)).tzinfo is UTC
    with pytest.raises(ValueError):
        clock.parse_time("soon")


def timezone_plus2():
    from datetime import timezone
    return timezone(timedelta(hours=2))


# ---- state -------------------------------------------------------------------------------------------------------


def test_state_is_a_small_json_store():
    state = State(":memory:")
    assert state.get("ns", "k") is None and state.get("ns", "k", 7) == 7
    state.set("ns", "k", {"a": [1, 2]})
    state.set("ns", "k", {"a": [3]})
    state.set("other", "k", "x")
    assert state.get("ns", "k") == {"a": [3]} and state.items("ns") == {"k": {"a": [3]}}
    state.delete("ns", "k")
    assert state.get("ns", "k") is None and state.get("other", "k") == "x"


def test_seen_is_true_once_per_key_and_prune_drops_old_entries():
    state = State(":memory:")
    assert state.seen("events", "e1") and not state.seen("events", "e1") and state.seen("events", "e2")
    clock.advance(days=8)
    state.set("events", "fresh", 1)
    assert state.prune("events", 7 * 24 * 3600) == 2
    assert list(state.items("events")) == ["fresh"]


def test_state_persists_in_a_wal_file_under_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VUMLO_PLUGIN_DATA", str(tmp_path / "data"))
    first = State()
    first.set("cache", "a", 1)
    assert (tmp_path / "data" / "state.db").is_file()
    first.close()
    again = State()
    assert again.get("cache", "a") == 1
    assert again._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    again.close()
    elsewhere = State(tmp_path / "x")
    assert (tmp_path / "x" / "state.db").is_file()
    elsewhere.close()


# ---- settings ----------------------------------------------------------------------------------------------------


async def test_settings_read_defaults_merge_and_cache(fake):
    plugin = Plugin("s", scopes=["settings:read"], settings={
        "unit": setting("string", enum=["words", "characters"], default="words"),
        "max_words": setting("number", minimum=1, scope="course"),
        "token": setting("string", secret=True),
    })
    fake.attach(plugin)
    course = fake.add_course()
    assert await plugin.config.get() == {"unit": "words"}
    fake.set_setting("unit", "characters")
    fake.set_setting("max_words", 10, course)
    fake.set_setting("token", "ghp_x")
    assert (await plugin.config.get())["unit"] == "words"  # cached
    plugin.config.invalidate()
    assert await plugin.config.get() == {"unit": "characters", "token": "ghp_x"}
    assert await plugin.config.get(course["id"]) == {"unit": "characters", "token": "ghp_x", "max_words": 10}
    reads = len([c for c in fake.calls if c.template == "/plugin-api/settings"])
    await plugin.config.get(course["id"])
    assert len([c for c in fake.calls if c.template == "/plugin-api/settings"]) == reads
    clock.advance(seconds=301)  # the cache lives five minutes
    await plugin.config.get(course["id"])
    assert len([c for c in fake.calls if c.template == "/plugin-api/settings"]) == reads + 1


async def test_settings_fall_back_to_defaults_when_the_core_cannot_answer_and_ignore_bad_values(fake):
    plugin = Plugin("s", scopes=["settings:read"], settings={"n": setting("number", default=3, maximum=5)})
    fake.attach(plugin)
    fake.grant(scopes=set())  # no settings:read
    assert await plugin.config.get() == {"n": 3}
    fake.grant(scopes={"settings:read"})
    fake.institution_values["n"] = 99  # something the spec would not take
    fake.institution_values["unknown"] = 1
    plugin.config.invalidate()
    assert await plugin.config.get() == {"n": 3}
    reader = SettingsReader(lambda: fake.client(), {"n": setting("number", default=3)})
    assert reader.defaults == {"n": 3}


async def test_a_failed_read_is_not_cached(fake):
    plugin = Plugin("s", scopes=["settings:read"], settings={"n": setting("number", default=3)})
    fake.attach(plugin)
    fake.fail("GET", "/plugin-api/settings", 503, times=10)
    assert await plugin.config.get() == {"n": 3}
    fake._failures.clear()
    fake.institution_values["n"] = 4
    assert await plugin.config.get() == {"n": 4}


# ---- logs --------------------------------------------------------------------------------------------------------


def test_logs_are_json_lines_without_personal_data():
    stream = io.StringIO()
    log = Log("logs-test", stream)
    log.info("synced", assignments=12, token="vplg_abc", email="a@b.c", note="mail ada@example.com now")
    log.warning("careful", header="Authorization: Bearer abc.def-123", gh="ghp_" + "a" * 30)
    try:
        raise RuntimeError("failed for ada@example.com")
    except RuntimeError:
        log.exception("boom", user_id="u1")
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0] == {"time": "2026-03-02T12:00:00Z", "level": "info", "plugin": "logs-test", "msg": "synced",
                        "assignments": 12, "token": "[redacted]", "email": "[redacted]", "note": "mail [email] now"}
    assert lines[1]["header"] == "Authorization: Bearer [token]" and lines[1]["gh"] == "[token]"
    assert lines[2]["level"] == "error" and lines[2]["error"] == "RuntimeError: failed for [email]"
    assert "ada@example.com" not in stream.getvalue() and "abc" not in lines[0]["token"]


def test_a_field_cannot_overwrite_the_envelope():
    stream = io.StringIO()
    Log("logs-test-2", stream).info("m", level="x", time="y", plugin="evil")
    line = json.loads(stream.getvalue())
    assert (line["msg"], line["level"], line["plugin"], line["time"]) == ("m", "info", "logs-test-2", "2026-03-02T12:00:00Z")


def test_scrub_masks_addresses_and_tokens():
    assert scrub("to a.b+c@sub.example.org, vplg_Abc-123_x") == "to [email], [token]"


# ---- scheduler ---------------------------------------------------------------------------------------------------


async def test_the_scheduler_runs_a_job_on_demand_and_reports_failures():
    scheduler = Scheduler(Log("sched-test", io.StringIO()))

    async def ok():
        return {"n": 2}

    async def silent():
        return None

    async def broken():
        raise ValueError("x")

    for name, fn in (("ok", ok), ("silent", silent), ("broken", broken)):
        scheduler.add(name, fn)
    assert await scheduler.run("ok") == {"job": "ok", "ok": True, "n": 2}
    assert await scheduler.run("silent") == {"job": "silent", "ok": True}
    assert await scheduler.run("broken") == {"job": "broken", "ok": False, "error": "ValueError"}


async def test_the_scheduler_validates_names_and_crontab_when_declared():
    scheduler = Scheduler(Log("sched-test-2", io.StringIO()), "Europe/Kyiv")
    scheduler.add("daily", lambda: None, cron="0 6 * * *")
    for bad in ("Daily", "1x", "a b", "x" * 41, ""):
        with pytest.raises(ValueError):
            scheduler.add(bad, lambda: None)
    with pytest.raises(ValueError, match="twice"):
        scheduler.add("daily", lambda: None)
    with pytest.raises(ValueError):
        scheduler.add("both", lambda: None, cron="0 6 * * *", every=5)
    with pytest.raises(ValueError):
        scheduler.add("badcron", lambda: None, cron="nope")
    scheduler.start()
    assert scheduler._scheduler is None or scheduler._scheduler.get_job("daily") is not None
    scheduler.stop()


async def test_cron_lines_follow_the_plugin_timezone():
    scheduler = Scheduler(Log("sched-test-3", io.StringIO()), "Europe/Kyiv")
    scheduler.add("daily", lambda: None, cron="0 6 * * *")
    scheduler.start()
    try:
        job = scheduler._scheduler.get_job("daily")
        assert str(job.trigger.timezone) == "Europe/Kyiv"
        assert job.next_run_time.hour == 6 and job.next_run_time.utcoffset() in (timedelta(hours=2), timedelta(hours=3))
    finally:
        scheduler.stop()


# ---- FakeCore helpers --------------------------------------------------------------------------------------------


async def test_the_fake_core_refuses_to_send_an_event_the_plugin_did_not_subscribe_to(fake):
    plugin = Plugin("quiet", scopes=[])
    fake.attach(plugin)
    with pytest.raises(AssertionError, match="does not subscribe"):
        await fake.deliver(plugin, "grade.published", {})
    assert (await fake.deliver(plugin, "grade.published", {}, force=True)).status_code == 202


def test_set_setting_checks_scope_and_value(fake):
    fake.setting_specs = {"a": setting("number", maximum=3, scope="course"), "b": setting("string")}
    course = fake.add_course()
    with pytest.raises(KeyError):
        fake.set_setting("c", 1)
    with pytest.raises(ValueError, match="course setting"):
        fake.set_setting("a", 1)
    with pytest.raises(ValueError, match="institution setting"):
        fake.set_setting("b", "x", course)
    with pytest.raises(ValueError, match="at most 3"):
        fake.set_setting("a", 9, course)


async def test_pagination_edges(fake):
    course = fake.add_course()
    for _ in range(3):
        fake.add_assignment(course)
    client = fake.client()
    first = await client.request("GET", "/plugin-api/assignments", params={"limit": 2})
    assert len(first["items"]) == 2 and first["next_cursor"]
    last = await client.request("GET", "/plugin-api/assignments", params={"limit": 2, "cursor": first["next_cursor"]})
    assert len(last["items"]) == 1 and last["next_cursor"] is None
    from vumlo_plugins import Invalid
    for params in ({"limit": 0}, {"limit": 101}, {"limit": "x"}, {"cursor": "garbage"}, {"updated_since": "yesterday"}):
        with pytest.raises(Invalid):
            await client.request("GET", "/plugin-api/assignments", params=params)


async def test_drafts_and_deleted_assignments_are_invisible_to_plugins(fake):
    course = fake.add_course()
    draft = fake.add_assignment(course, published=False)
    client = fake.client()
    assert await client.assignments() == []
    from vumlo_plugins import NotFound
    with pytest.raises(NotFound):
        await client.assignment(draft["id"])
    fake.delete_assignment(draft)  # a draft leaves no tombstone
    assert await client.deleted_assignments("2026-03-01T00:00:00Z") == []
