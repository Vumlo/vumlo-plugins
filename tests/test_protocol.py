import time

from vumlo_plugins.protocol import event_signature, verify_event


def test_bad_signature_is_rejected():
    body = b'{"id":"e1","type":"submission.submitted","data":{}}'
    headers = {"X-Vumlo-Timestamp": str(int(time.time())), "X-Vumlo-Signature": "sha256=bad"}
    assert verify_event("secret", headers, body)[0] == 401


def test_valid_signature_is_accepted():
    body = b"{}"
    timestamp = "1700000000"
    headers = {"X-Vumlo-Timestamp": timestamp, "X-Vumlo-Signature": event_signature("secret", timestamp, body)}
    assert verify_event("secret", headers, body, now=1700000000) is None
