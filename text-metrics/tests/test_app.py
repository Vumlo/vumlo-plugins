from vumlo_plugins.protocol import event_signature
from app import process

def test_word_count(monkeypatch):
    class Client:
        def submission(self, _): return {"content": {"type": "text", "text": "one two three"}}
        def settings(self, _): return {"minimum_words": 4, "maximum_words": 5}
        def report(self, *args): self.args = args
    client = Client()
    monkeypatch.setattr("app.client", client)
    process("submission.submitted", {"data": {"submission_id": "s", "course_id": "c"}})
    # report(subject, key, summary, data)
    assert client.args[1] == "text_metrics"
    assert client.args[2] == "3 words"
    assert client.args[3]["minimum_ok"] is False
    assert client.args[3]["maximum_ok"] is True
