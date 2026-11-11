"""The port of the core's manifest validator (`manifest.rs`, `settings.rs`), and a check that the manifest of every
plugin in this repository would be accepted by `vumlo-cli plugin install`."""

import importlib
import json
from pathlib import Path

import pytest

from vumlo_plugins import ManifestError, Plugin, route, setting, validate_manifest
from vumlo_plugins.manifest import SCOPES, SUBSCRIBABLE, check_setting_value, manifest

ROOT = Path(__file__).parent.parent


def good(**over):
    base = manifest("demo", "1.0.0", subscribes=["submission.submitted"], scopes=["submissions:read"])
    base.update(over)
    return base


def refused(raw, fragment: str):
    with pytest.raises(ManifestError) as error:
        validate_manifest(raw)
    assert fragment in str(error.value)


# ---- every plugin of the repository ------------------------------------------------------------------------------


def plugin_folders() -> list[Path]:
    return sorted(p.parent for p in ROOT.glob("*/app.py"))


@pytest.mark.parametrize("folder", plugin_folders(), ids=lambda p: p.name)
def test_the_manifest_of_every_plugin_is_accepted_by_the_core_rules(folder: Path):
    module = importlib.import_module(f"{folder.name}.app")
    plugin = module.app.state.plugin
    assert isinstance(plugin, Plugin)
    served = plugin.manifest()
    assert served["id"] == folder.name, "the folder, the plugin id and the compose service plugin-<id> are one name"
    assert validate_manifest(json.loads(json.dumps(served))) == served
    assert (folder / "README.md").is_file()
    assert (folder / "tests").is_dir()
    for scope in served["scopes"]:
        assert scope in SCOPES
    if plugin.scheduler.jobs:  # Contract 5: scheduled work can be run now by an administrator
        assert any(r["path"] == "/jobs/{name}" and r["roles"] == ["admin"] for r in served["routes"])


def test_this_repository_has_no_folder_that_is_not_a_plugin_or_known_tooling():
    known = {"vumlo_plugins", "tests", "tools", "docs", "uv.lock"}
    for entry in ROOT.iterdir():
        if entry.is_dir() and not entry.name.startswith(".") and entry.name not in known:
            assert (entry / "app.py").is_file(), f"{entry.name} has no app.py: create-vumlo would not offer it"


# ---- the validator -----------------------------------------------------------------------------------------------


def test_a_good_manifest_is_returned_as_the_core_stores_it():
    raw = good(subscribes=["submission.submitted", "submission.submitted"], scopes=["reports:write", "reports:write"])
    out = validate_manifest(raw)
    assert out["subscribes"] == ["submission.submitted"] and out["scopes"] == ["reports:write"]
    assert out["api"] == "1" and out["routes"] == [] and out["ui"] == [] and out["settings"] == {}


@pytest.mark.parametrize("plugin_id", ["", "Upper", "1abc", "has space", "x" * 33, "ünï", "a.b", "_a"])
def test_a_bad_id_is_refused(plugin_id):
    refused(good(id=plugin_id), "must be lowercase letters")


def test_ids_of_32_characters_with_digits_dash_and_underscore_are_fine():
    validate_manifest(good(id="a" + "b_-0" * 7 + "z"))
    validate_manifest(good(id="x" * 32))


def test_version_and_api():
    refused(good(version=""), "version")
    refused(good(version="x" * 33), "version")
    refused(good(version="1\n0"), "version")
    refused(good(api="2"), "not supported")
    refused({**good(), "version": 1}, "not the JSON object")
    refused([], "not the JSON object")


def test_subscriptions():
    refused(good(subscribes=["grade.deleted"]), "cannot subscribe to `grade.deleted`")
    refused(good(subscribes=["submission.submitted"] * 11), "more than 10")
    validate_manifest(good(subscribes=list(SUBSCRIBABLE)))  # all nine, including the settings event
    refused(good(subscribes="submission.submitted"), "list of strings")


def test_scopes_include_the_mail_scope_and_nothing_unknown():
    assert "mail:send" in SCOPES
    validate_manifest(good(scopes=list(SCOPES)))
    refused(good(scopes=["admin:all"]), "scope `admin:all` is unknown")


def test_ui_pages():
    page = {"slot": "assignment.tab", "title": " Words ", "path": "/ui/words"}
    assert validate_manifest(good(ui=[page]))["ui"][0]["title"] == "Words"
    refused(good(ui=[{**page, "slot": "course.tab"}]), "unknown")
    refused(good(ui=[{**page, "title": "  "}]), "title")
    refused(good(ui=[{**page, "title": "x" * 61}]), "title")
    for path in ("ui", "//evil.example", "/a/../b", "/a b", "/a\\b", "/" + "a" * 200):
        refused(good(ui=[{**page, "path": path}]), "must start with a single `/`")
    refused(good(ui=[page, page]), "two pages")
    refused(good(ui=[{"slot": "assignment.tab"}]), "not the JSON object")


# ---- routes ------------------------------------------------------------------------------------------------------


def with_route(**kw):
    spec = {"method": "GET", "path": "/x", "roles": ["admin"], **kw}
    return good(routes=[spec])


def test_a_session_route_is_normalised():
    out = validate_manifest(with_route(method="get", roles=["admin", "admin", "teacher"]))["routes"][0]
    assert out == {"method": "GET", "auth": "session", "path": "/x", "roles": ["admin", "teacher"], "course_roles": []}


def test_route_methods():
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "post"):
        validate_manifest(with_route(method=method))
    for method in ("HEAD", "OPTIONS", "TRACE", "", "GET "):
        refused(with_route(method=method), "route method")


def test_route_paths():
    for path in ("/export/{course_id}/xlsx", "/a/b.c/d~e-f_g", "/{a}/{b_c}", "/jobs/{name}", "/" + "a" * 199):
        validate_manifest(with_route(path=path))
    refused(with_route(path="x"), "must start with `/`")
    refused(with_route(path="/" + "a" * 200), "at most 200")
    refused(with_route(path="/"), "at least one segment")
    refused(with_route(path="/a//b"), "a segment holds")
    refused(with_route(path="/a/"), "a segment holds")
    refused(with_route(path="/a/../b"), "a segment holds")
    refused(with_route(path="/a/./b"), "a segment holds")
    refused(with_route(path="/a b"), "a segment holds")
    refused(with_route(path="/ü"), "a segment holds")
    refused(with_route(path="/a?x=1"), "a segment holds")
    refused(with_route(path="/{Id}"), "lowercase letters and `_`, once")
    refused(with_route(path="/{id1}"), "lowercase letters and `_`, once")
    refused(with_route(path="/{}"), "lowercase letters and `_`, once")
    refused(with_route(path="/{a}/{a}"), "lowercase letters and `_`, once")
    refused(with_route(path="/{" + "a" * 21 + "}"), "lowercase letters and `_`, once")
    refused(with_route(path="/a{id}"), "a segment holds")


def test_roles_are_required_and_known():
    refused(with_route(roles=[]), "roles must name at least one")
    refused(with_route(roles=["owner"]), "role `owner` is not admin, teacher or student")
    refused(with_route(roles=["teacher"], course_roles=["owner"], path="/c/{course_id}"), "course role")
    out = validate_manifest(with_route(path="/c/{course_id}", course_roles=["teacher", "assistant", "teacher"]))
    assert out["routes"][0]["course_roles"] == ["teacher", "assistant"]


def test_course_roles_need_the_course_segment():
    refused(with_route(course_roles=["teacher"]), "need a `{course_id}` segment")


def test_a_public_route_is_a_get_without_roles_or_course():
    ok = validate_manifest(good(routes=[{"method": "GET", "path": "/feed/{token}", "auth": "none"}]))["routes"][0]
    assert ok == {"method": "GET", "auth": "none", "path": "/feed/{token}", "roles": [], "course_roles": []}
    refused(good(routes=[{"method": "POST", "path": "/f", "auth": "none"}]), "is GET only")
    refused(good(routes=[{"method": "GET", "path": "/f", "auth": "none", "roles": ["admin"]}]), "has no roles")
    refused(good(routes=[{"method": "GET", "path": "/f", "auth": "none", "course_roles": ["teacher"]}]), "has no roles")
    refused(good(routes=[{"method": "GET", "path": "/f/{course_id}", "auth": "none"}]), "no `{course_id}` segment")
    refused(with_route(auth="basic"), "is not session or none")
    refused(good(routes=[{"method": "GET", "path": "/x", "auth": 1}]), "not the JSON object")


def test_route_limits_and_duplicates():
    twenty = [{"method": "GET", "path": f"/r{i}", "roles": ["admin"]} for i in range(20)]
    validate_manifest(good(routes=twenty))
    refused(good(routes=twenty + [{"method": "GET", "path": "/more", "roles": ["admin"]}]), "more than 20 routes")
    refused(good(routes=[twenty[0], twenty[0]]), "declared twice")
    refused(good(routes=[twenty[0], {**twenty[0], "method": "get"}]), "declared twice")
    validate_manifest(good(routes=[twenty[0], {**twenty[0], "method": "POST"}]))
    refused(good(routes=[{"method": "GET"}]), "not the JSON object")


def test_the_builders_make_what_the_validator_takes():
    spec = route("GET", "/export/{course_id}/csv", roles=["teacher"], course_roles=["teacher"])
    validate_manifest(good(routes=[spec, route("GET", "/feed/{t}", auth="none")]))


# ---- settings ----------------------------------------------------------------------------------------------------


def with_setting(key="limit", **spec):
    return good(settings={key: spec})


def test_setting_keys():
    for key in ("a", "min_words", "a1_b2", "k" * 40):
        validate_manifest(with_setting(key, type="string"))
    for key in ("", "A", "1a", "a-b", "k" * 41, "ü"):
        refused(with_setting(key, type="string"), "must be lowercase letters, digits and `_`")
    refused(good(settings={f"k{i}": {"type": "string"} for i in range(41)}), "more than 40 settings")


def test_setting_types_and_unknown_fields():
    refused(with_setting(type="date"), "`type` must be")
    refused(good(settings={"a": "string"}), "expected a map")
    refused(with_setting(type="string", colour="red"), "unknown field `colour`")
    refused(with_setting(type="string", description=3), "`description` is text")
    refused(with_setting(type="string", scope="user"), "institution or course")


def test_setting_numbers_enums_and_arrays():
    validate_manifest(with_setting(type="number", minimum=1, maximum=10, default=5, scope="course"))
    refused(with_setting(type="string", minimum=1), "for numbers")
    refused(with_setting(type="number", minimum=5, maximum=1), "`minimum` is above `maximum`")
    refused(with_setting(type="number", minimum="1"), "are numbers")
    refused(with_setting(type="number", default=11, maximum=10), "default must be at most 10")
    refused(with_setting(type="number", default=True), "default must be a number")
    validate_manifest(with_setting(type="string", enum=["a", "b"], default="a"))
    refused(with_setting(type="string", enum=["a", "a"]), "distinct plain strings")
    refused(with_setting(type="string", enum=[""]), "distinct plain strings")
    refused(with_setting(type="string", enum=["x" * 101]), "distinct plain strings")
    refused(with_setting(type="string", enum=[]), "1 to 50")
    refused(with_setting(type="string", enum=[str(i) for i in range(51)]), "1 to 50")
    refused(with_setting(type="number", enum=["a"]), "`enum` is for strings")
    refused(with_setting(type="string", enum=["a"], default="b"), "default must be one of: a")
    validate_manifest(with_setting(type="array", items={"type": "string"}, default=["x"]))
    refused(with_setting(type="array"), "`items` goes with the type `array`")
    refused(with_setting(type="string", items={"type": "string"}), "`items` goes with the type `array`")
    refused(with_setting(type="array", items={"type": "boolean"}), "`items` is")
    refused(with_setting(type="array", items={"type": "number"}, default=["a"]), "list of numbers")
    refused(with_setting(type="array", items={"type": "string"}, default=["a"] * 51), "at most 50 entries")


def test_secrets():
    validate_manifest(with_setting(type="string", secret=True))
    refused(with_setting(type="string", secret=True, default="x"), "a secret has no default")
    refused(with_setting(type="number", secret=True), "only a string without `enum`")
    refused(with_setting(type="string", secret=True, enum=["a"]), "only a string without `enum`")


def test_descriptions_are_plain_text_of_at_most_300_characters():
    validate_manifest(with_setting(type="string", description="x" * 300))
    refused(with_setting(type="string", description="x" * 301), "description must be plain text")
    refused(with_setting(type="string", description="a\nb"), "description must be plain text")


def test_check_setting_value():
    spec = setting("number", minimum=1, maximum=3)
    assert check_setting_value(spec, 2) is None and check_setting_value(spec, 3.5)
    assert check_setting_value(spec, "2") and check_setting_value(spec, True)
    assert check_setting_value(setting("string", secret=True), "") is not None
    assert check_setting_value(setting("string"), "") is None
    assert check_setting_value(setting("string"), "a\x00b")
    assert check_setting_value(setting("boolean"), False) is None and check_setting_value(setting("boolean"), 0)
    assert check_setting_value(setting("array", items="number"), [1, 2.5]) is None
    assert check_setting_value(setting("array", items="number"), "1")


def test_a_plugin_that_has_settings_subscribes_to_the_settings_event_by_itself():
    plugin = Plugin("demo", scopes=["settings:read"], settings={"unit": setting("string", default="words")})
    assert "plugin.settings.changed" in plugin.manifest()["subscribes"]


def test_a_plugin_with_a_manifest_the_core_would_refuse_is_refused_at_start():
    plugin = Plugin("Bad Id")
    with pytest.raises(ManifestError):
        plugin.manifest()
    ok = Plugin("ok")
    with pytest.raises(ManifestError):  # a public route with a course segment, caught when it is declared
        ok.route("GET", "/x/{course_id}", auth="none")(lambda *a, **k: None)
        ok.manifest()
    with pytest.raises(ValueError):
        Plugin("ok").job("Bad Name", every=1)(lambda: None)
    with pytest.raises(ValueError):
        Plugin("ok").job("both", every=1, cron="* * * * *")(lambda: None)
    with pytest.raises(ValueError):
        Plugin("ok").job("bad-cron", cron="61 * * * *")(lambda: None)
