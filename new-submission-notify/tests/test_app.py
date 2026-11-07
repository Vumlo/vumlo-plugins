from app import MANIFEST
def test_subscribes_to_existing_event(): assert "submission.submitted" in MANIFEST["subscribes"]
