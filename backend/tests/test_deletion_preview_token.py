from __future__ import annotations

import pytest

from app.services import deletions


def test_delete_preview_token_is_bound_to_user_resource_and_hash() -> None:
    token = deletions._token("email", 7, "a" * 64, 3)
    deletions._validate_token(
        token,
        resource_type="email",
        resource_id=7,
        preview_hash="a" * 64,
        user_id=3,
    )
    with pytest.raises(deletions.DeletionError, match="DELETE_PREVIEW_STALE"):
        deletions._validate_token(
            token,
            resource_type="email",
            resource_id=8,
            preview_hash="a" * 64,
            user_id=3,
        )


def test_preview_hash_is_stable_and_changes_with_closure() -> None:
    first = deletions._canonical_hash({"resource_id": 1, "counts": {"email": 1}})
    reordered = deletions._canonical_hash({"counts": {"email": 1}, "resource_id": 1})
    changed = deletions._canonical_hash({"resource_id": 1, "counts": {"email": 2}})
    assert first == reordered
    assert first != changed
