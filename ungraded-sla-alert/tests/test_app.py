from app import MANIFEST
def test_existing_events_only(): assert set(MANIFEST["subscribes"]) == {"submission.submitted", "grade.published"}
