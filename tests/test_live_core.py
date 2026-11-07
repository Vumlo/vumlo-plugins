"""Opt-in integration test against a running Vumlo core.

Required environment:
  VUMLO_CORE_URL   Core API origin, e.g. http://127.0.0.1:8080/api/v1
  VUMLO_CLI        Path to the built vumlo-cli
  DATABASE_URL     PostgreSQL URL used by the running core
  VUMLO_PLUGINS_SECRET_KEY  Same key used by the running core

The test uses psql only to enqueue a real outbox event. The running core's
event worker and plugin delivery worker perform the signed delivery.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parents[1]


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is required for the live-core integration test")
    return value


def _install(cli: str, url: str, grant: str) -> tuple[str, str]:
    result = subprocess.run(
        [cli, "plugin", "install", url, "--grant", grant],
        capture_output=True,
        text=True,
        env={**os.environ, "VUMLO__PLUGINS__SECRET_KEY": _required("VUMLO_PLUGINS_SECRET_KEY")},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        for key in ("VUMLO_PLUGIN_TOKEN", "VUMLO_WEBHOOK_SECRET"):
            if line.startswith(f"{key}="):
                values[key] = line.split("=", 1)[1]
    assert values.keys() >= {"VUMLO_PLUGIN_TOKEN", "VUMLO_WEBHOOK_SECRET"}, result.stdout
    return values["VUMLO_PLUGIN_TOKEN"], values["VUMLO_WEBHOOK_SECRET"]


def _create_user(cli: str, config: str, email: str, role: str) -> tuple[str, str]:
    server = os.getenv("VUMLO_SERVER", str(Path(cli).with_name("vumlo-server")))
    result = subprocess.run(
        [server, "--config", config, "create-user", email, "--name", role.title(), "--surname", "Integration", "--role", role],
        capture_output=True,
        text=True,
        env={**os.environ, "VUMLO__PLUGINS__SECRET_KEY": _required("VUMLO_PLUGINS_SECRET_KEY")},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    user_id = re.search(r"Created \w+ ([0-9a-f-]+)", result.stdout + result.stderr).group(1)
    password = re.search(r"Temporary password: (\S+)", result.stdout + result.stderr).group(1)
    return user_id, password


def _login(client: httpx.Client, base: str, email: str, password: str) -> tuple[str, dict]:
    response = client.post(f"{base}/auth/login", json={"email": email, "password": password, "client": "app"})
    response.raise_for_status()
    login = response.json()
    token = login["token"]
    response = client.post(
        f"{base}/me/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": password, "new_password": "IntegrationPassword9!"},
    )
    response.raise_for_status()
    response = client.post(
        f"{base}/auth/login",
        json={"email": email, "password": "IntegrationPassword9!", "client": "app"},
    )
    response.raise_for_status()
    login = response.json()
    return login["token"], login["user"]


def _start(plugin_dir: str, port: int, token: str, secret: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "VUMLO_PLUGIN_TOKEN": token,
        "VUMLO_WEBHOOK_SECRET": secret,
        "VUMLO_API": os.environ.get("VUMLO_CORE_URL", "").rstrip("/"),
        "PYTHONPATH": str(ROOT),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT / plugin_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_health(url: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"{url} did not become ready")


@pytest.mark.integration
def test_install_scope_delivery_and_bad_signature_against_live_core():
    core_url = _required("VUMLO_CORE_URL").rstrip("/")
    cli = _required("VUMLO_CLI")
    database_url = os.getenv("DATABASE_URL", "")
    custom_psql = os.getenv("VUMLO_PSQL_COMMAND")
    psql_command = shlex.split(custom_psql or "psql")
    webhook_port = 19101
    github_port = 19102
    metrics_port = 19103
    webhook_url = f"http://127.0.0.1:{webhook_port}"
    github_url = f"http://127.0.0.1:{github_port}"
    metrics_url = f"http://127.0.0.1:{metrics_port}"
    webhook = github = None
    metrics = None
    manifest_webhook = manifest_github = manifest_metrics = None
    plugin_ids = ["webhook-forwarder", "github-repo-check", "text-metrics"]
    try:
        assert httpx.get(f"{core_url}/health", timeout=5).status_code == 200
        manifest_webhook = _start("webhook-forwarder", webhook_port, "", "")
        manifest_github = _start("github-repo-check", github_port, "", "")
        manifest_metrics = _start("text-metrics", metrics_port, "", "")
        _wait_health(webhook_url)
        _wait_health(github_url)
        _wait_health(metrics_url)
        webhook_token, webhook_secret = _install(cli, webhook_url, "none")
        github_token, github_secret = _install(
            cli, github_url, "submissions:read,reports:write"
        )
        metrics_token, metrics_secret = _install(
            cli, metrics_url, "submissions:read,reports:write,settings:read"
        )
        listed = subprocess.run(
            [cli, "plugin", "list"],
            capture_output=True,
            text=True,
            env={**os.environ, "VUMLO__PLUGINS__SECRET_KEY": _required("VUMLO_PLUGINS_SECRET_KEY")},
            check=True,
        )
        assert "github-repo-check" in listed.stdout
        assert "submissions:read, reports:write" in listed.stdout
        for process in (manifest_webhook, manifest_github):
            process.terminate()
            process.wait(timeout=5)
        manifest_webhook = manifest_github = None
        manifest_metrics.terminate()
        manifest_metrics.wait(timeout=5)
        manifest_metrics = None
        webhook = _start("webhook-forwarder", webhook_port, webhook_token, webhook_secret)
        github = _start("github-repo-check", github_port, github_token, github_secret)
        metrics = _start("text-metrics", metrics_port, metrics_token, metrics_secret)
        _wait_health(webhook_url)
        _wait_health(github_url)
        _wait_health(metrics_url)

        event_id = secrets.token_hex(16)
        payload = json.dumps(
            {
                "id": event_id,
                "type": "submission.submitted",
                "data": {
                    "assignment_id": secrets.token_hex(16),
                    "student_id": secrets.token_hex(16),
                    "course_id": secrets.token_hex(16),
                    "submission_id": secrets.token_hex(16),
                },
            }
        )
        subprocess.run(
            [
                *psql_command,
                *([database_url] if database_url and not custom_psql else []),
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "INSERT INTO events.outbox (id, type, version, payload) "
                f"VALUES (gen_random_uuid(), 'submission.submitted', 1, '{payload}'::jsonb)",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            webhook_events = httpx.get(f"{webhook_url}/health").json()["received_events"]
            github_events = httpx.get(f"{github_url}/health").json()["received_events"]
            if webhook_events >= 1 and github_events >= 1:
                break
            time.sleep(0.5)
        assert webhook_events >= 1
        assert github_events >= 1

        config = _required("VUMLO_CONFIG")
        suffix = secrets.token_hex(5)
        teacher_email = f"teacher-{suffix}@example.edu"
        student_email = f"student-{suffix}@example.edu"
        teacher_id, teacher_temp = _create_user(cli, config, teacher_email, "teacher")
        student_id, student_temp = _create_user(cli, config, student_email, "student")
        with httpx.Client(timeout=10) as client:
            teacher_token, _ = _login(client, core_url, teacher_email, teacher_temp)
            teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
            response = client.post(f"{core_url}/courses", headers=teacher_headers, json={"title": f"Metrics {suffix}"})
            response.raise_for_status()
            course_id = response.json()["id"]
            response = client.post(f"{core_url}/courses/{course_id}/modules", headers=teacher_headers, json={"title": "Work"})
            response.raise_for_status()
            module_id = response.json()["id"]
            response = client.post(
                f"{core_url}/modules/{module_id}/items",
                headers=teacher_headers,
                json={
                    "title": "Text submission",
                    "kind": "assignment",
                    "body": {"v": 1, "blocks": []},
                    "assignment": {
                        "type": "text",
                        "max_points": "10",
                        "max_attempts": 1,
                        "due_at": None,
                        "on_deadline": None,
                        "accept_late": None,
                        "late_until": None,
                        "is_bonus": False,
                        "config": {},
                        "class_on": None,
                    },
                },
            )
            response.raise_for_status()
            assignment_id = response.json()["id"]
            response = client.put(
                f"{core_url}/courses/{course_id}/members/{student_id}",
                headers=teacher_headers,
                json={"role": "student"},
            )
            response.raise_for_status()
            publish_module = client.post(f"{core_url}/modules/{module_id}/publish", headers=teacher_headers, json={"cascade": True})
            assert publish_module.is_success, publish_module.text
            publish_course = client.post(f"{core_url}/courses/{course_id}/publish", headers=teacher_headers)
            assert publish_course.is_success, publish_course.text

            settings = subprocess.run(
                [
                    cli, "--config", config, "plugin", "settings", "set",
                    "text-metrics", "minimum_words", "4", "--course", course_id,
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "VUMLO__PLUGINS__SECRET_KEY": _required("VUMLO_PLUGINS_SECRET_KEY")},
                check=True,
            )
            assert "set `minimum_words`" in settings.stdout
            student_token, _ = _login(client, core_url, student_email, student_temp)
            student_headers = {"Authorization": f"Bearer {student_token}"}
            response = client.put(
                f"{core_url}/assignments/{assignment_id}/submission/draft",
                headers=student_headers,
                json={"content": {"v": 1, "blocks": [{"id": "p1", "type": "paragraph", "content": [{"type": "text", "text": "one two three"}]}]}},
            )
            response.raise_for_status()
            response = client.post(f"{core_url}/assignments/{assignment_id}/submission/submit", headers=student_headers)
            response.raise_for_status()
            submission_id = response.json()["id"]

            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if httpx.get(f"{metrics_url}/health").json()["received_events"] >= 1:
                    reports = client.get(
                        f"{core_url}/submissions/{submission_id}/plugin-reports",
                        headers=teacher_headers,
                    )
                    if reports.status_code == 200 and reports.json()["items"]:
                        break
                time.sleep(0.5)
            reports.raise_for_status()
            report = next(item for item in reports.json()["items"] if item["plugin_id"] == "text-metrics")
            assert report["data"]["words"] == 3
            assert report["data"]["minimum"] == 4
            assert report["data"]["minimum_ok"] is False

        for url in (webhook_url, github_url, metrics_url):
            bad = httpx.post(
                f"{url}/events",
                content=b"{}",
                headers={
                    "X-Vumlo-Timestamp": str(int(time.time())),
                    "X-Vumlo-Signature": "sha256=invalid",
                },
            )
            assert bad.status_code == 401
    finally:
        for process in (webhook, github, metrics, manifest_webhook, manifest_github, manifest_metrics):
            if process:
                process.terminate()
                process.wait(timeout=5)
        for plugin_id in plugin_ids:
            subprocess.run(
                [cli, "plugin", "remove", plugin_id, "--yes"],
                env={**os.environ, "VUMLO__PLUGINS__SECRET_KEY": os.getenv("VUMLO_PLUGINS_SECRET_KEY", "")},
                capture_output=True,
                text=True,
            )
