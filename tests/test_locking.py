import pytest

from fiscal_rag.locking import IndexingLock


def test_prevents_concurrent_indexing(tmp_path) -> None:
    path = tmp_path / "indexing.lock"
    with IndexingLock(path):
        with pytest.raises(RuntimeError, match="already running"):
            with IndexingLock(path):
                pass

    with IndexingLock(path):
        pass
