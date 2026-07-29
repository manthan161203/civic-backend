"""
``PATCH /admin/announcements/{id}`` — editing without re-notifying.
==================================================================

Announcements could be created and deleted but never edited. The admin console
shipped a complete Edit modal wired to a toast that said the backend did not
support it, so fixing a typo in a message already delivered to a district meant
deleting it and posting a replacement — which pushed to everyone a second time.

The design decision this endpoint forces, and which the tests below pin:

``push_dispatched_at`` is the latch the ``jobs`` service reads to decide who
still needs a push. Clearing it on edit would re-deliver to every matching
citizen, so a one-character correction would buzz a district again. It is
therefore left alone, and returned in the response so the console can say so
rather than leave an admin guessing.

Re-scoping is not offered at all: the citizens who received a ward announcement
are not the citizens who would receive it as a district one, so changing scope
is a delete plus a new post, not an edit.
"""

from datetime import timedelta

from app.core.time import now_utc
from app.models.announcement import Announcement

from tests.conftest import auth_header, make_user


def _make_announcement(db, author, **kwargs):
    defaults = dict(
        title="Water supply interrupted",
        body="Supply will be off from 09:00 to 14:00.",
        author_id=author.id,
        scope="state",
    )
    defaults.update(kwargs)
    ann = Announcement(**defaults)
    db.add(ann)
    db.flush()
    return ann


def _patch(client, user, ann_id, body):
    return client.patch(
        f"/admin/announcements/{ann_id}", json=body, headers=auth_header(user)
    )


# ── the edit works ────────────────────────────────────────────────────────────


def test_author_can_edit_title_and_body(client, db, super_admin):
    ann = _make_announcement(db, super_admin)

    response = _patch(
        client, super_admin, ann.id, {"title": "Corrected", "body": "09:00 to 16:00."}
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Corrected"

    db.refresh(ann)
    assert ann.title == "Corrected"
    assert ann.body == "09:00 to 16:00."


def test_partial_edit_leaves_other_fields_alone(client, db, super_admin):
    ann = _make_announcement(db, super_admin, body="Original body")

    response = _patch(client, super_admin, ann.id, {"title": "New title only"})
    assert response.status_code == 200, response.text

    db.refresh(ann)
    assert ann.body == "Original body"


def test_expiry_can_be_extended(client, db, super_admin):
    ann = _make_announcement(db, super_admin, expires_at=now_utc() + timedelta(days=1))
    later = now_utc() + timedelta(days=7)

    response = _patch(client, super_admin, ann.id, {"expires_at": later.isoformat()})
    assert response.status_code == 200, response.text

    db.refresh(ann)
    assert ann.expires_at > now_utc() + timedelta(days=6)


def test_expiry_can_be_cleared_with_an_explicit_null(client, db, super_admin):
    """`exclude_unset` is what makes this possible.

    With `exclude_none` an explicit null would be indistinguishable from an
    omitted field, and there would be no way to un-expire an announcement or
    remove a map pin.
    """
    ann = _make_announcement(db, super_admin, expires_at=now_utc() + timedelta(days=1))

    response = _patch(client, super_admin, ann.id, {"expires_at": None})
    assert response.status_code == 200, response.text

    db.refresh(ann)
    assert ann.expires_at is None


def test_updated_at_moves(client, db, super_admin):
    ann = _make_announcement(db, super_admin)
    db.commit()
    before = ann.updated_at

    response = _patch(client, super_admin, ann.id, {"title": "Edited"})
    assert response.status_code == 200, response.text

    db.refresh(ann)
    assert ann.updated_at >= before


# ── editing must not re-notify ────────────────────────────────────────────────


def test_editing_does_not_clear_the_push_latch(client, db, super_admin):
    """The whole point. Clearing it would re-push to everyone."""
    dispatched = now_utc() - timedelta(hours=3)
    ann = _make_announcement(db, super_admin, push_dispatched_at=dispatched)

    response = _patch(client, super_admin, ann.id, {"body": "Corrected timing."})
    assert response.status_code == 200, response.text

    db.refresh(ann)
    assert ann.push_dispatched_at is not None, (
        "clearing this would re-deliver the announcement to every matching citizen"
    )
    assert ann.push_dispatched_at == dispatched


def test_response_reports_whether_it_was_already_delivered(client, db, super_admin):
    """The console needs this to warn that an edit will not reach anyone."""
    ann = _make_announcement(db, super_admin, push_dispatched_at=now_utc())

    response = _patch(client, super_admin, ann.id, {"title": "Edited"})
    assert response.json()["push_dispatched_at"] is not None

    undelivered = _make_announcement(db, super_admin)
    response = _patch(client, super_admin, undelivered.id, {"title": "Edited"})
    assert response.json()["push_dispatched_at"] is None


def test_scope_cannot_be_changed(client, db, super_admin, hierarchy):
    """Re-scoping a delivered announcement is a new post, not an edit.

    Unknown keys are ignored by the request model rather than rejected, so the
    assertion is on the stored row: the scope must not move.
    """
    ann = _make_announcement(db, super_admin, scope="state")

    response = _patch(
        client,
        super_admin,
        ann.id,
        {"scope": "ward", "ward_id": str(hierarchy["ward"].id), "title": "Edited"},
    )
    assert response.status_code == 200, response.text

    db.refresh(ann)
    assert ann.scope == "state"
    assert ann.ward_id is None
    assert ann.title == "Edited", "the legitimate part of the edit still applied"


# ── authorization matches DELETE ──────────────────────────────────────────────


def test_another_admin_cannot_edit_someone_elses_announcement(client, db, hierarchy):
    author = make_user(db, "ward_admin", ward_id=hierarchy["ward"].id)
    other = make_user(db, "ward_admin", ward_id=hierarchy["other_ward"].id)
    ann = _make_announcement(db, author, scope="ward", ward_id=hierarchy["ward"].id)

    response = _patch(client, other, ann.id, {"title": "Hijacked"})
    assert response.status_code == 403, response.text

    db.refresh(ann)
    assert ann.title == "Water supply interrupted"


def test_super_admin_can_edit_anyones_announcement(client, db, hierarchy, super_admin):
    author = make_user(db, "ward_admin", ward_id=hierarchy["ward"].id)
    ann = _make_announcement(db, author, scope="ward", ward_id=hierarchy["ward"].id)

    response = _patch(client, super_admin, ann.id, {"title": "Moderated"})
    assert response.status_code == 200, response.text


def test_a_citizen_cannot_reach_the_endpoint(client, db, citizen, super_admin):
    ann = _make_announcement(db, super_admin)

    response = _patch(client, citizen, ann.id, {"title": "Nope"})
    assert response.status_code == 403, response.text


# ── validation ────────────────────────────────────────────────────────────────


def test_unknown_announcement_is_404(client, super_admin):
    import uuid

    response = _patch(client, super_admin, uuid.uuid4(), {"title": "x"})
    assert response.status_code == 404


def test_empty_body_is_rejected(client, db, super_admin):
    """An edit that changes nothing is a mistake worth surfacing."""
    ann = _make_announcement(db, super_admin)

    response = _patch(client, super_admin, ann.id, {})
    assert response.status_code == 400, response.text


def test_blank_title_is_rejected(client, db, super_admin):
    ann = _make_announcement(db, super_admin)

    response = _patch(client, super_admin, ann.id, {"title": ""})
    assert response.status_code == 422, response.text

    db.refresh(ann)
    assert ann.title == "Water supply interrupted"
