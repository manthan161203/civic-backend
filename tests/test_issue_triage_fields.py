"""
``PATCH /issues/{id}`` — priority and department are written, not swallowed.
===========================================================================

``IssueUpdate`` declared ``priority`` and ``department``, documented the first
as "(admin only)", validated both against enums — and ``update_issue`` never
read either one. The endpoint answered ``200 OK`` with the issue unchanged.

That is a worse failure than a 422: the admin console showed the value it had
just sent (it re-rendered from its own optimistic state), the next page load
showed the old value, and nothing anywhere recorded a problem. Re-triaging an
issue was a no-op that looked like a success.

Both columns already exist on ``Issue`` — ``priority`` as a native enum and
``department`` as an indexed string — so this is purely a matter of the handler
reading fields it was already being handed.

Ordering matters and is pinned below: ``priority`` is written *before* the
status branch, because that branch reads ``issue.priority`` to decide whether an
after-photo is required. Writing it afterwards would evaluate the guard against
the value being replaced.
"""

import pytest

from tests.conftest import auth_header
from tests.test_phase8_endpoints import _make_issue


def _patch(client, user, issue_id, body):
    return client.patch(f"/issues/{issue_id}", json=body, headers=auth_header(user))


# ── admins can re-triage ──────────────────────────────────────────────────────


def test_admin_sets_priority(client, db, citizen, super_admin):
    issue = _make_issue(db, citizen, priority="low")

    response = _patch(client, super_admin, issue.id, {"priority": "urgent"})
    assert response.status_code == 200, response.text
    assert response.json()["priority"] == "urgent"

    db.refresh(issue)
    assert issue.priority == "urgent", "the response must not report a write that did not happen"


def test_admin_sets_department(client, db, citizen, super_admin):
    issue = _make_issue(db, citizen)

    response = _patch(client, super_admin, issue.id, {"department": "sanitation"})
    assert response.status_code == 200, response.text

    db.refresh(issue)
    assert issue.department == "sanitation"


def test_ward_admin_in_scope_can_retriage(client, db, citizen, ward_admin, hierarchy):
    """Scoped admins, not only the super-admin — they are the ones who triage."""
    issue = _make_issue(db, citizen, ward_id=hierarchy["ward"].id, priority="low")

    response = _patch(client, ward_admin, issue.id, {"priority": "high"})
    assert response.status_code == 200, response.text

    db.refresh(issue)
    assert issue.priority == "high"


def test_ward_admin_cannot_retriage_outside_their_ward(
    client, db, citizen, other_ward_admin, hierarchy
):
    issue = _make_issue(db, citizen, ward_id=hierarchy["ward"].id, priority="low")

    response = _patch(client, other_ward_admin, issue.id, {"priority": "urgent"})
    assert response.status_code == 403, response.text

    db.refresh(issue)
    assert issue.priority == "low"


def test_priority_and_department_can_change_together(client, db, citizen, super_admin):
    issue = _make_issue(db, citizen, priority="low")

    response = _patch(
        client, super_admin, issue.id, {"priority": "high", "department": "roads"}
    )
    assert response.status_code == 200, response.text

    db.refresh(issue)
    assert (issue.priority, issue.department) == ("high", "roads")


# ── everyone else cannot ──────────────────────────────────────────────────────


def test_citizen_cannot_set_priority_on_their_own_issue(client, db, citizen):
    """Owning the issue is not the same as being allowed to triage it."""
    issue = _make_issue(db, citizen, priority="low")

    response = _patch(client, citizen, issue.id, {"priority": "urgent"})
    assert response.status_code == 200, response.text

    db.refresh(issue)
    assert issue.priority == "low", "a citizen must not be able to jump their own queue"


def test_worker_cannot_set_priority_on_their_assigned_task(client, db, citizen, worker):
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned", priority="low")

    response = _patch(client, worker, issue.id, {"priority": "urgent"})
    assert response.status_code == 200, response.text

    db.refresh(issue)
    assert issue.priority == "low"


# ── validation and side effects ───────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["critical", "URGENT", "none", ""])
def test_unknown_priority_is_rejected(client, db, citizen, super_admin, bad):
    """`critical` is in this list deliberately.

    The admin web app's priority filter offered low/medium/high/**critical**
    while the column enum is urgent/high/medium/low — so the filter could never
    match a row, and there was no way to filter for urgent at all. The API must
    reject the name rather than accept and ignore it.
    """
    issue = _make_issue(db, citizen, priority="low")

    response = _patch(client, super_admin, issue.id, {"priority": bad})
    assert response.status_code == 422, response.text

    db.refresh(issue)
    assert issue.priority == "low"


def test_unknown_department_is_rejected(client, db, citizen, super_admin):
    issue = _make_issue(db, citizen)

    response = _patch(client, super_admin, issue.id, {"department": "telecoms"})
    assert response.status_code == 422, response.text


def test_omitting_the_fields_leaves_them_alone(client, db, citizen, super_admin):
    """`None` means "not supplied", not "clear it"."""
    issue = _make_issue(db, citizen, priority="high", department="water")

    response = _patch(client, super_admin, issue.id, {"resolution_notes": "unrelated edit"})
    assert response.status_code == 200, response.text

    db.refresh(issue)
    assert (issue.priority, issue.department) == ("high", "water")


def test_changing_department_does_not_reassign_the_worker(
    client, db, citizen, worker, super_admin
):
    """`department` participates in auto-assignment matching, so a change leaves
    the current worker matched on the old one. Silently reassigning would pull a
    task out from under someone mid-job; the assignment is left for an admin to
    revisit deliberately.
    """
    issue = _make_issue(
        db, citizen, assigned_worker_id=worker.id, status="assigned", department="water"
    )

    response = _patch(client, super_admin, issue.id, {"department": "roads"})
    assert response.status_code == 200, response.text

    db.refresh(issue)
    assert issue.department == "roads"
    assert issue.assigned_worker_id == worker.id
    assert issue.status == "assigned"


def test_priority_is_applied_before_the_status_branch(client, db, citizen, super_admin):
    """The after-photo guard in the status branch reads `issue.priority`.

    Applying the new priority after that branch would evaluate the guard against
    the value being replaced. This pins the ordering: raising priority to urgent
    and moving status in one request must leave both written.
    """
    issue = _make_issue(db, citizen, status="open", priority="low")

    response = _patch(
        client, super_admin, issue.id, {"priority": "urgent", "status": "in_progress"}
    )
    assert response.status_code == 200, response.text

    db.refresh(issue)
    assert (issue.priority, issue.status) == ("urgent", "in_progress")
