import pytest

from app.services.storage import LocalStorage


def test_local_storage_round_trip(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    storage.put_bytes("results/job/result.json", b"{}", "application/json")
    assert storage.exists("results/job/result.json")
    assert storage.get_bytes("results/job/result.json") == b"{}"


@pytest.mark.parametrize("key", ["../secret", "/absolute/path", "results/../../secret"])
def test_local_storage_rejects_unsafe_keys(tmp_path, key: str) -> None:
    storage = LocalStorage(tmp_path)
    with pytest.raises(ValueError):
        storage.put_bytes(key, b"x", "text/plain")
