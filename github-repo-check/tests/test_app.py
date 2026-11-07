from app import MANIFEST
def test_secret_is_declared(): assert MANIFEST["settings"]["github_token"]["secret"] is True
