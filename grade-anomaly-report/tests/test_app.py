from app import MANIFEST
def test_existing_event(): assert MANIFEST["subscribes"] == ["grade.published"]
