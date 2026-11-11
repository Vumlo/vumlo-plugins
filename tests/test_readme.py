"""The README's thirty-line plugin is the file `readme_example.py`, and it works."""

from pathlib import Path

from tests.readme_example import plugin
from vumlo_plugins import validate_manifest

ROOT = Path(__file__).parent.parent


def test_the_readme_shows_the_example_file_verbatim():
    readme = (ROOT / "vumlo_plugins" / "README.md").read_text()
    shown = readme.split("<!-- example:start -->")[1].split("<!-- example:end -->")[0]
    code = shown.split("```python\n", 1)[1].rsplit("```", 1)[0]
    assert code == (ROOT / "tests" / "readme_example.py").read_text()
    assert len(code.strip().splitlines()) <= 31


async def test_the_readme_example_works(fake):
    fake.attach(plugin)
    validate_manifest(plugin.manifest())
    course = fake.add_course()
    student = fake.add_user("Sam", "Student")
    fake.enroll(course, student)
    work = fake.add_assignment(course)
    fake.set_setting("max_words", 2, course)
    sub = fake.add_submission(work, student, text="one two three")
    await fake.deliver(plugin, "submission.submitted", fake.submission_event(sub))
    assert fake.report("submission", sub, "word-count")["summary"] == "3 words"
    assert [m["kind"] for m in fake.mails_to(student)] == ["too_long"]
    assert (await fake.call(plugin, "GET", "/whoami", user=student)).json()["role"] == "student"
