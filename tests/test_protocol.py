import time

from vumlo_plugins.protocol import event_signature, verify_event, verify_proxy


def test_bad_signature_is_rejected():
    body = b'{"id":"e1","type":"submission.submitted","data":{}}'
    headers = {"X-Vumlo-Timestamp": str(int(time.time())), "X-Vumlo-Signature": "sha256=bad"}
    assert verify_event("secret", headers, body)[0] == 401


def test_valid_signature_is_accepted():
    body = b"{}"
    timestamp = "1700000000"
    headers = {"X-Vumlo-Timestamp": timestamp, "X-Vumlo-Signature": event_signature("secret", timestamp, body)}
    assert verify_event("secret", headers, body, now=1700000000) is None


def test_proxy_identity_signature_is_verified():
    body = b'{"ok":true}'
    timestamp = "1700000000"
    nonce = "n-1"
    user = "u-1"
    role = "teacher"
    course = "c-1"
    course_role = "teacher"
    import hashlib
    canonical = "\n".join([
        "vumlo-proxy", nonce, "GET", "/feed", "session", user, role,
        course, course_role, hashlib.sha256(body).hexdigest(),
    ])
    headers = {
        "x-vumlo-timestamp": timestamp,
        "x-vumlo-nonce": nonce,
        "x-vumlo-user": user,
        "x-vumlo-role": role,
        "x-vumlo-course": course,
        "x-vumlo-course-role": course_role,
        "x-vumlo-signature": event_signature("secret", timestamp, canonical.encode()),
    }
    assert verify_proxy("secret", headers, body, method="GET", target="/feed", now=1700000000) == {
        "user_id": user, "role": role, "course_id": course, "course_role": course_role,
    }
