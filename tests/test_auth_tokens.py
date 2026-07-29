"""
Token and session regression tests (Phase 7.3, 7.4).
====================================================
"""

import uuid

from app.core.security import ACCESS_TOKEN_TYPE, create_access_token, decode_access_token
from app.core.time import now_utc
from app.services.auth_service import create_refresh_token

from tests.conftest import auth_header, make_user


# ── 7.3 — token type confusion ────────────────────────────────────────────────


def test_access_token_carries_type_claim():
    payload = decode_access_token(create_access_token({"sub": str(uuid.uuid4())}))
    assert payload is not None
    assert payload["type"] == ACCESS_TOKEN_TYPE
    assert "iat" in payload, "iat is required for the tokens_valid_from cutoff"


def test_refresh_token_is_rejected_as_access_token(client, citizen, db):
    """A refresh token presented as a Bearer credential must not authenticate.

    Both are signed with the same SECRET_KEY and algorithm; only the `type`
    claim distinguishes them, and nothing checked it. So a stolen refresh token
    granted full API access for its **30-day** lifetime instead of the access
    token's 60 minutes — and kept working after logout, because the access path
    never consults refresh_tokens.is_revoked.
    """
    refresh = create_refresh_token(str(citizen.id), db)
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert response.status_code == 401


def test_access_token_still_works(client, citizen):
    response = client.get("/auth/me", headers=auth_header(citizen))
    assert response.status_code == 200
    assert response.json()["id"] == str(citizen.id)


def test_garbage_token_is_rejected(client):
    assert client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401


# ── 7.4 — session invalidation ────────────────────────────────────────────────


def test_tokens_valid_from_retires_existing_access_tokens(client, citizen, db):
    """Bumping the cutoff must invalidate tokens issued before it.

    This is what makes "I've been compromised, change my password" actually do
    something for stateless access tokens.
    """
    header = auth_header(citizen)
    assert client.get("/auth/me", headers=header).status_code == 200

    # Cutoff one second in the future, so the token above is unambiguously older.
    from datetime import timedelta

    citizen.tokens_valid_from = now_utc() + timedelta(seconds=1)
    db.flush()

    assert client.get("/auth/me", headers=header).status_code == 401


def test_password_change_revokes_all_sessions(client, db):
    """change_password wrote the new hash and nothing else.

    An attacker holding a stolen refresh token kept rotating it indefinitely
    after the victim changed their password.
    """
    from app.core.security import hash_password
    from app.models.refresh_token import RefreshToken

    user = make_user(db, "citizen", password_hash=hash_password("OldPassw0rd!"))
    create_refresh_token(str(user.id), db)
    assert db.query(RefreshToken).filter(RefreshToken.user_id == user.id, RefreshToken.is_revoked == False).count() == 1  # noqa: E712

    response = client.post(
        "/auth/change-password",
        headers=auth_header(user),
        json={
            "current_password": "OldPassw0rd!",
            "new_password": "NewPassw0rd!",
            "confirm_password": "NewPassw0rd!",
        },
    )
    assert response.status_code == 200, response.text

    db.refresh(user)
    assert user.tokens_valid_from is not None
    live = db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id, RefreshToken.is_revoked == False  # noqa: E712
    ).count()
    assert live == 0, "refresh tokens survived a password change"


# ── 7.4 — deactivated worker self-reactivation ────────────────────────────────


def test_deactivated_worker_cannot_self_reactivate(client, db):
    """Deactivation left must_change_password set, so login flipped is_active back on.

    A fired worker simply logged in with the temp password they still had and
    the account came back, undoing revoke_all_user_tokens along with it.
    """
    from app.core.security import hash_password

    worker = make_user(
        db,
        "worker",
        password_hash=hash_password("TempPassw0rd!"),
        is_active=False,
        must_change_password=True,
        invitation_sent_at=None,  # what deactivation now sets
    )
    response = client.post(
        "/auth/login",
        json={"identifier": worker.phone, "password": "TempPassw0rd!"},
    )
    assert response.status_code == 403
    db.refresh(worker)
    assert worker.is_active is False


def test_pending_worker_with_live_invitation_can_activate(client, db):
    """The legitimate first-login activation still works."""
    from app.core.security import hash_password

    worker = make_user(
        db,
        "worker",
        password_hash=hash_password("TempPassw0rd!"),
        is_active=False,
        must_change_password=True,
        invitation_sent_at=now_utc(),
    )
    response = client.post(
        "/auth/login",
        json={"identifier": worker.phone, "password": "TempPassw0rd!"},
    )
    assert response.status_code == 200, response.text
    db.refresh(worker)
    assert worker.is_active is True


def test_admin_deactivation_clears_the_invitation(client, super_admin, db):
    """Deactivating must close the reactivation path, not just flip is_active."""
    worker = make_user(
        db,
        "worker",
        is_active=True,
        must_change_password=True,
        invitation_sent_at=now_utc(),
    )
    response = client.post(
        f"/admin/workers/{worker.id}/deactivate", headers=auth_header(super_admin)
    )
    assert response.status_code == 200, response.text
    db.refresh(worker)
    assert worker.is_active is False
    assert worker.must_change_password is False
    assert worker.invitation_sent_at is None
    assert worker.tokens_valid_from is not None
