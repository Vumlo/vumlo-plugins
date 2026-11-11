"""The signatures, against vectors computed by the Rust definitions (`secret.rs`, `proxy.rs`).

The vectors below are the ones the core's own tests assert (`plugins/src/secret/tests.rs`), so a change on either
side that breaks the byte-for-byte agreement fails a test on both sides.
"""

import hashlib
import hmac

import pytest

from vumlo_plugins import clock
from vumlo_plugins.signing import (Identity, NonceStore, Refused, proxy_canonical, proxy_signature, sign, verify_event,
                                   verify_proxy)
from vumlo_plugins.testing import signed_call_headers, signed_event

BODY = b'{"id":"e1","type":"submission.submitted","data":{"submission_id":"s1"}}'
SECRET = "test-secret"
T0 = 1_700_000_000
USER = "018f0000-0000-7000-8000-000000000003"
COURSE = "018f0000-0000-7000-8000-000000000004"
NONCE = "018f0000-0000-7000-8000-000000000005"


def test_event_signature_vector_of_the_core():
    assert sign(SECRET, T0, BODY) == "sha256=93d5d9c8289fe4a0066a2634f57443ea75081452aec5ad86e7310daeaf07e941"


def test_proxy_signature_vectors_of_the_core():
    kwargs = dict(nonce=NONCE, user=USER)
    assert proxy_signature(SECRET, T0, method="POST", target="/hello/api/echo?x=1", role="teacher", course=COURSE,
                           course_role="assistant", body=b'{"a":1}', **kwargs) \
        == "sha256=b44aaebf5b3727dcdaee5f5de6da4e02eb960d31f16ed99ab83df89c45fa3d39"
    assert proxy_signature(SECRET, T0, method="GET", target="/api/summary/x", role="admin", body=b"", **kwargs) \
        == "sha256=c4e553deaf5580300420b744170981ed9f596adcaae94581419ccd2642e87595"
    # Nobody signed in: a public route.
    assert proxy_signature(SECRET, T0, nonce=NONCE, method="GET", target="/hello/api/feed/f?token=t") \
        == "sha256=4a7ce61d44771dc438866c26db839b3513e197e8a9c846ba489bb2340cccc978"


def test_the_canonical_lines_are_the_documented_ones():
    canonical = proxy_canonical(nonce=NONCE, method="post", target="/t", user=USER, role="teacher", course=COURSE,
                                course_role="assistant", body=b"x")
    assert canonical.decode().split("\n") == [
        "vumlo-proxy", NONCE, "POST", "/t", "session", USER, "teacher", COURSE, "assistant",
        hashlib.sha256(b"x").hexdigest()]
    nobody = proxy_canonical(nonce=NONCE, method="GET", target="/t", user=None, role="admin", course=None,
                             course_role=None, body=b"")
    assert nobody.decode().split("\n")[4:] == ["none", "-", "-", "-", "-", hashlib.sha256(b"").hexdigest()]


def test_sign_is_hmac_sha256_of_timestamp_dot_body():
    expected = hmac.new(b"k", b"1700000000." + b"abc", hashlib.sha256).hexdigest()
    assert sign("k", 1_700_000_000, b"abc") == f"sha256={expected}"
    assert sign("k", "1700000000", b"abc") == f"sha256={expected}"


def test_a_proxied_call_changes_its_signature_with_every_part():
    base = dict(nonce=NONCE, method="POST", target="/t?x=1", user=USER, role="teacher", course=COURSE,
                course_role="assistant", body=b'{"a":1}')
    reference = proxy_signature(SECRET, T0, **base)
    changes = {"nonce": "other", "method": "PUT", "target": "/t?x=2", "user": "u2", "role": "admin", "course": "c2",
               "course_role": "teacher", "body": b"{}"}
    for name, value in changes.items():
        assert proxy_signature(SECRET, T0, **{**base, name: value}) != reference, name
    assert proxy_signature(SECRET, T0 + 1, **base) != reference
    assert proxy_signature("other", T0, **base) != reference
    assert proxy_signature(SECRET, T0, **{**base, "user": None}) != reference


# ---- verification --------------------------------------------------------------------------------------------


def headers_for(body: bytes = BODY, at: int = T0, secret: str = SECRET) -> dict[str, str]:
    return {"X-Vumlo-Timestamp": str(at), "X-Vumlo-Signature": sign(secret, at, body)}


def test_verify_event_accepts_a_genuine_fresh_event_whatever_the_header_case():
    verify_event(SECRET, headers_for(), BODY, now=T0 + 10)
    verify_event(SECRET, {k.lower(): v for k, v in headers_for().items()}, BODY, now=T0 - 299)


@pytest.mark.parametrize("change,status,why", [
    (lambda h, b: (h, b + b" "), 401, "bad signature"),
    (lambda h, b: ({**h, "X-Vumlo-Signature": "sha256=" + "0" * 64}, b), 401, "bad signature"),
    (lambda h, b: ({k: v for k, v in h.items() if k != "X-Vumlo-Signature"}, b), 401, "bad signature"),
    (lambda h, b: ({**h, "X-Vumlo-Timestamp": "soon"}, b), 401, "bad timestamp"),
    (lambda h, b: ({k: v for k, v in h.items() if k != "X-Vumlo-Timestamp"}, b), 401, "bad timestamp"),
])
def test_verify_event_refuses(change, status, why):
    headers, body = change(headers_for(), BODY)
    with pytest.raises(Refused) as refused:
        verify_event(SECRET, headers, body, now=T0)
    assert (refused.value.status, refused.value.why) == (status, why)


def test_verify_event_refuses_a_stale_or_future_timestamp_and_a_missing_secret():
    for now in (T0 + 301, T0 - 301):
        with pytest.raises(Refused, match="stale"):
            verify_event(SECRET, headers_for(), BODY, now=now)
    with pytest.raises(Refused) as refused:
        verify_event("", headers_for(), BODY, now=T0)
    assert refused.value.status == 503


def test_verify_event_uses_the_frozen_clock_by_default():
    with clock.frozen("2023-11-14T22:13:20Z"):  # == T0
        verify_event(SECRET, headers_for(), BODY)
    with clock.frozen("2023-11-14T23:13:20Z"), pytest.raises(Refused, match="stale"):
        verify_event(SECRET, headers_for(), BODY)


def call_headers(**kwargs) -> dict[str, str]:
    kwargs.setdefault("nonce", NONCE)
    kwargs.setdefault("timestamp", T0)
    return signed_call_headers(SECRET, kwargs.pop("method", "GET"), kwargs.pop("target", "/x"), **kwargs)


def test_verify_proxy_returns_the_signed_identity():
    headers = call_headers(method="POST", target="/api/echo?x=1", user=USER, role="teacher", course=COURSE,
                           course_role="assistant", body=b'{"a":1}')
    who = verify_proxy(SECRET, headers, b'{"a":1}', method="POST", target="/api/echo?x=1", now=T0)
    assert who == Identity(USER, "teacher", COURSE, "assistant")
    assert not who.anonymous and not who.is_admin and who.is_staff_of_course


def test_verify_proxy_of_a_public_call_is_anonymous():
    who = verify_proxy(SECRET, call_headers(), b"", method="GET", target="/x", now=T0)
    assert who.anonymous and who == Identity()


def test_verify_proxy_matches_the_core_vector_end_to_end():
    headers = {"x-vumlo-timestamp": str(T0), "x-vumlo-nonce": NONCE, "x-vumlo-user": USER, "x-vumlo-role": "teacher",
               "x-vumlo-course": COURSE, "x-vumlo-course-role": "assistant",
               "x-vumlo-signature": "sha256=b44aaebf5b3727dcdaee5f5de6da4e02eb960d31f16ed99ab83df89c45fa3d39"}
    who = verify_proxy(SECRET, headers, b'{"a":1}', method="POST", target="/hello/api/echo?x=1", now=T0)
    assert who.user_id == USER and who.course_role == "assistant"


@pytest.mark.parametrize("name,value", [
    ("x-vumlo-user", "someone-else"), ("x-vumlo-role", "admin"), ("x-vumlo-course", "c2"),
    ("x-vumlo-course-role", "teacher"), ("x-vumlo-nonce", "n2"),
])
def test_verify_proxy_refuses_a_header_changed_after_signing(name, value):
    headers = call_headers(user=USER, role="student", course=COURSE, course_role="student")
    headers[name] = value
    with pytest.raises(Refused) as refused:
        verify_proxy(SECRET, headers, b"", method="GET", target="/x", now=T0)
    assert refused.value.status == 401


def test_verify_proxy_refuses_what_the_signature_does_not_cover():
    headers = call_headers(user=USER, role="student")
    for kwargs in (dict(method="POST", target="/x"), dict(method="GET", target="/y"), dict(method="GET", target="/x?a=1")):
        with pytest.raises(Refused, match="bad signature"):
            verify_proxy(SECRET, headers, b"", now=T0, **kwargs)
    with pytest.raises(Refused, match="bad signature"):
        verify_proxy(SECRET, headers, b"body", method="GET", target="/x", now=T0)


def test_a_person_cannot_be_passed_off_as_anonymous_or_the_other_way_round():
    person = call_headers(user=USER, role="admin")
    del person["x-vumlo-user"], person["x-vumlo-role"]
    person["x-vumlo-anonymous"] = "1"
    with pytest.raises(Refused):
        verify_proxy(SECRET, person, b"", method="GET", target="/x", now=T0)
    anonymous = call_headers()
    del anonymous["x-vumlo-anonymous"]
    anonymous.update({"x-vumlo-user": USER, "x-vumlo-role": "admin"})
    with pytest.raises(Refused):
        verify_proxy(SECRET, anonymous, b"", method="GET", target="/x", now=T0)
    neither = call_headers()
    del neither["x-vumlo-anonymous"]
    with pytest.raises(Refused):
        verify_proxy(SECRET, neither, b"", method="GET", target="/x", now=T0)


def test_an_event_signature_is_not_a_proxy_signature():
    headers = {"x-vumlo-timestamp": str(T0), "x-vumlo-nonce": NONCE, "x-vumlo-anonymous": "1",
               "x-vumlo-signature": sign(SECRET, T0, b"")}
    with pytest.raises(Refused):
        verify_proxy(SECRET, headers, b"", method="GET", target="/x", now=T0)


def test_a_nonce_is_taken_once_and_old_ones_are_forgotten():
    nonces = NonceStore(limit=2)
    headers = call_headers(user=USER, role="student")
    verify_proxy(SECRET, headers, b"", method="GET", target="/x", nonces=nonces, now=T0)
    with pytest.raises(Refused, match="seen before"):
        verify_proxy(SECRET, headers, b"", method="GET", target="/x", nonces=nonces, now=T0 + 1)
    nonces.take("b", T0)
    with pytest.raises(Refused) as full:
        nonces.take("c", T0)
    assert full.value.status == 503
    nonces.take("c", T0 + 2000)  # the old ones aged out


def test_verify_proxy_refuses_without_a_secret_and_when_stale():
    with pytest.raises(Refused) as refused:
        verify_proxy("", call_headers(), b"", method="GET", target="/x", now=T0)
    assert refused.value.status == 503
    with pytest.raises(Refused, match="stale"):
        verify_proxy(SECRET, call_headers(), b"", method="GET", target="/x", now=T0 + 1000)


def test_signed_event_helper_produces_what_verify_event_takes():
    body, headers = signed_event(SECRET, "grade.published", {"assignment_id": "a"}, event_id="e9", timestamp=T0)
    verify_event(SECRET, headers, body, now=T0)
    assert b'"id":"e9"' in body and b'"type":"grade.published"' in body
