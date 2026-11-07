from vumlo_plugins.protocol import event_signature
from app import process

def test_word_count(monkeypatch):
    class Client:
        def submission(self, _): return {"content": {"type": "text", "text": "one two three"}}
        def report(self, *args): self.args = args
    client = Client()
    monkeypatch.setattr("app.client", client)
    process("submission.submitted", {"data": {"submission_id": "s"}})
    assert client.args[1] == "3 words"
