"""The shapes `FakeCore` serves, checked against the core's generated OpenAPI (`openapi_plugin_api.json`, cut from
`vumlo-core/web/openapi.json` by `tools/refresh_openapi.py`) and against the mail contract (Contract 1).

If the core changes a DTO, refresh the OpenAPI file and this test says what the fake must follow.
"""

import httpx
import pytest

from vumlo_plugins import Gone, Invalid, NotFound, Report
from vumlo_plugins.testing import FakeCore, contract

pytestmark = pytest.mark.skipif(not contract.available(), reason="jsonschema is a dev dependency")

# Not served on purpose: it hands out addresses, and plugins send mail through the core instead (Contract 1).
NOT_FAKED = {("GET", "/api/v1/plugin-api/users/contact")}


@pytest.fixture
def populated(fake: FakeCore):
    course = fake.add_course("Algebra")
    teacher = fake.add_user("Tara", "Teacher", role="teacher")
    fake.enroll(course, teacher, "teacher")
    student = fake.add_user("Sam", "Student")
    fake.enroll(course, student)
    work = fake.add_assignment(course, title="Essay", due_at="2026-03-10T09:00:00Z")
    form = fake.add_assignment(course, kind="form", fields=[{"key": "q", "label": "Q", "type": "text"}])
    quiz = fake.add_assignment(course, kind="quiz")
    fake.add_quiz(quiz, {"questions": [{"id": "q1", "type": "single", "points": 1,
                                        "options": [{"id": "a", "text": "A", "correct": True}]}]})
    sub = fake.add_submission(work, student, text="hello")
    form_sub = fake.add_submission(form, student, content={"answers": {"q": "yes"}})
    fake.add_attempt(quiz, student, {"q1": 1})
    fake.publish_grade(work, student, 91.5, published_by=teacher)
    fake.add_announcement(course, "Hello", "Welcome", teacher)
    extra = fake.add_assignment(course)
    fake.delete_assignment(extra)
    fake.setting_specs = {"limit": {"type": "number", "default": 5}}
    return fake, course, work, quiz, sub, form_sub


def routes_of(fake: FakeCore) -> set[tuple[str, str]]:
    return {(m, r.path) for r in fake.app.routes for m in r.methods if m != "HEAD"}


def test_the_fake_serves_every_route_the_core_documents_for_plugins(fake):
    documented = {(method.upper(), path) for path, ops in contract.spec()["paths"].items() for method in ops}
    served = routes_of(fake) - {("POST", f"{contract.API_PREFIX}/plugin-api/mail")}
    assert documented - NOT_FAKED == served, "refresh the OpenAPI file, or serve what is missing"
    assert ("POST", contract.MAIL_PATH) in routes_of(fake)


async def get_all(fake: FakeCore, course, work, quiz, sub, form_sub):
    """Every documented GET, with the parameters that make the answer non-empty."""
    p = f"{contract.API_PREFIX}/plugin-api"
    paths = [
        f"{p}/assignments?course_id={course['id']}", f"{p}/assignments/{work['id']}",
        f"{p}/assignments/deleted?deleted_since=2026-03-01T00:00:00Z",
        f"{p}/assignments/{work['id']}/submissions", f"{p}/assignments/{quiz['id']}/quiz",
        f"{p}/assignments/{quiz['id']}/attempts", f"{p}/submissions/{sub['id']}",
        f"{p}/submissions/{form_sub['id']}/fields", f"{p}/courses/{course['id']}/members?role=student",
        f"{p}/grades?course_id={course['id']}&assignment_id={work['id']}", f"{p}/announcements",
        f"{p}/announcements/deleted?deleted_since=2026-03-01T00:00:00Z", f"{p}/settings?course_id={course['id']}",
    ]
    out = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app), base_url="http://core") as http:
        for path in paths:
            response = await http.get(path, headers={"Authorization": f"Bearer {fake.token}"})
            assert response.status_code == 200, (path, response.text)
            out[path] = response.json()
    return out


async def test_every_answer_matches_the_openapi_schema(populated):
    fake, *world = populated
    answers = await get_all(fake, *world)
    assert answers  # the fake validates each request and answer itself (validate=True); reaching here means they fit
    assert len(fake.calls) == len(answers) and all(c.status == 200 for c in fake.calls)
    # and the answers are not empty shells
    items = [a for a in answers.values() if "items" in a]
    assert sum(1 for a in items if a["items"]) >= 6


async def test_reports_written_and_read_by_staff_match_the_schema(populated):
    fake, course, work, quiz, sub, form_sub = populated
    client = fake.client()
    for kind, subject in (("submission", sub), ("assignment", work), ("course", course)):
        await client.reports.put(kind, subject["id"], "k", Report.build("x", level="ok", columns=["a"], rows=[["b"]]))
        await client.reports.delete(kind, subject["id"], "k")
        await client.reports.put(kind, subject["id"], "k", Report.build("again"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app), base_url="http://core") as http:
        for segment, subject in (("submissions", sub), ("assignments", work), ("courses", course)):
            response = await http.get(f"{contract.API_PREFIX}/{segment}/{subject['id']}/plugin-reports")
            assert response.status_code == 200 and response.json()["items"][0]["key"] == "k"
    assert {c.status for c in fake.calls if c.method in ("PUT", "DELETE")} == {204}


async def test_problem_answers_match_the_schema(populated):
    fake, course, work, quiz, sub, form_sub = populated
    client = fake.client()
    with pytest.raises(NotFound):
        await client.assignment("00000000-0000-4000-8000-0000000fffff")
    with pytest.raises(Gone):
        await client.deleted_assignments("2020-01-01T00:00:00Z")
    with pytest.raises(Invalid):  # the client checks the summary, so go around it to see the core's 422
        await client.request("PUT", f"/plugin-api/courses/{course['id']}/reports/k", json={"summary": "", "data": {}})
    assert {c.status for c in fake.calls} == {404, 410, 422}


def test_the_checker_rejects_what_the_schema_does_not_say():
    op = contract.operation("GET", "/plugin-api/assignments/{id}")
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    good = {"id": "00000000-0000-4000-8000-000000000001", "course_id": "00000000-0000-4000-8000-000000000002",
            "kind": "text", "title": "t", "due_at": None, "max_points": "100", "updated_at": "2026-03-02T12:00:00Z"}
    contract.check(schema, good, "good")
    for broken, why in (({**good, "extra": 1}, "extra"), ({k: v for k, v in good.items() if k != "title"}, "title"),
                        ({**good, "id": "nope"}, "uuid"), ({**good, "updated_at": "yesterday"}, "date")):
        with pytest.raises(AssertionError):
            contract.check(schema, broken, why)


def test_the_checker_knows_routes_and_query_parameters():
    contract.check_request("GET", "/plugin-api/assignments", ["course_id", "cursor", "limit", "updated_since"])
    with pytest.raises(AssertionError, match="no query parameter"):
        contract.check_request("GET", "/plugin-api/assignments", ["since"])
    with pytest.raises(AssertionError, match="no GET /plugin-api/nothing"):
        contract.check_request("GET", "/plugin-api/nothing", [])


def test_the_mail_contract_schemas():
    to_one = {"to": {"user_id": "00000000-0000-4000-8000-000000000001"}, "kind": "deadline_changed",
              "subject": "s", "text": "t", "link_path": "/courses/1", "dedupe_key": "k"}
    to_all = {"to": {"course_id": "00000000-0000-4000-8000-000000000001", "audience": "all"}, "kind": "k",
              "subject": "s", "text": "t"}
    contract.check_body("POST", "/plugin-api/mail", to_one)
    contract.check_body("POST", "/plugin-api/mail", to_all)
    bad = [
        {**to_one, "to": {"user_id": "x"}}, {**to_one, "kind": "Bad"}, {**to_one, "subject": ""},
        {**to_one, "link_path": "//evil"}, {**to_one, "dedupe_key": "k" * 101}, {**to_one, "text": "t" * 20001},
        {**to_all, "to": {"course_id": to_all["to"]["course_id"], "audience": "parents"}},
        {**to_one, "to": {"user_id": to_one["to"]["user_id"], "email": "a@b.c"}}, {**to_one, "cc": "a@b.c"},
        {k: v for k, v in to_one.items() if k != "text"},
    ]
    for body in bad:
        with pytest.raises(AssertionError):
            contract.check_body("POST", "/plugin-api/mail", body)
    reasons = {"opted_out": 0, "no_address": 0, "duplicate": 0, "disabled": 0}
    contract.check_response("POST", "/plugin-api/mail", 202, {"accepted": 1, "skipped": 0, "skipped_reasons": reasons})
    for answer in ({"accepted": 1, "skipped": 0}, {"accepted": 1, "skipped": 0, "skipped_reasons": {"opted_out": 0}},
                   {"accepted": -1, "skipped": 0, "skipped_reasons": reasons}):
        with pytest.raises(AssertionError):
            contract.check_response("POST", "/plugin-api/mail", 202, answer)


async def test_the_fake_refuses_a_request_the_core_would_not_understand(fake):
    client = fake.client()
    with pytest.raises(AssertionError, match="no query parameter"):
        await client.request("GET", "/plugin-api/assignments", params={"since": "x"})
