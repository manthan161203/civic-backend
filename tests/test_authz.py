"""
Authorization regression tests (Phase 7).
=========================================

Every test here corresponds to a hole that was open in the running system. The
name of each test says what an attacker could do before the fix.
"""

import uuid

import pytest

from tests.conftest import auth_header, make_user


# ── 7.1 — app/routes/optimization.py ──────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/bulk/assign-issues"),
        ("post", "/bulk/mark-resolved"),
        ("post", "/tasks/cleanup-resolved"),
        ("get", "/search/advanced"),
        ("get", "/analytics/dashboard"),
        ("get", "/metrics/performance"),
    ],
)
def test_optimization_endpoints_are_gone(client, citizen, method, path):
    """The unguarded bulk/cleanup router must not be reachable at all.

    These were registered with no prefix and no role check. Any authenticated
    citizen could POST /tasks/cleanup-resolved?days=1 and hard-DELETE every
    resolved issue in the system, or POST /bulk/mark-resolved with a list of
    UUIDs harvested from the public map and close every open complaint.
    """
    kwargs = {"headers": auth_header(citizen)}
    if method == "post":
        kwargs["json"] = []
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 404, (
        f"{method.upper()} {path} is still routed — returned {response.status_code}"
    )


# ── 7.2 — self-service privilege escalation ───────────────────────────────────


def test_ward_admin_cannot_reassign_own_ward(client, ward_admin, hierarchy, db):
    """PUT /auth/profile must not let an admin choose their own jurisdiction.

    ward_id/taluka_id/district_id on the User row are where
    get_admin_scope_filter derives all admin authority from. Writing them from
    a self-service profile endpoint meant a ward_admin for Ward A could take
    over Ward B with one request.
    """
    original = ward_admin.ward_id
    response = client.put(
        "/auth/profile",
        headers=auth_header(ward_admin),
        json={"ward_id": str(hierarchy["other_ward"].id)},
    )
    assert response.status_code == 403
    db.refresh(ward_admin)
    assert ward_admin.ward_id == original


def test_taluka_and_district_are_not_accepted_from_profile(client, citizen):
    """The two most dangerous scope fields are gone from the schema entirely."""
    response = client.put(
        "/auth/profile",
        headers=auth_header(citizen),
        json={"taluka_id": str(uuid.uuid4()), "district_id": str(uuid.uuid4())},
    )
    # Unknown fields are ignored by pydantic, so the request succeeds — the
    # assertion that matters is that nothing was written.
    assert response.status_code == 200
    body = response.json()
    assert body.get("taluka_id") is None
    assert body.get("district_id") is None


def test_citizen_can_still_set_home_ward(client, citizen, hierarchy, db):
    """The legitimate use of ward_id — a citizen's home ward — still works."""
    response = client.put(
        "/auth/profile",
        headers=auth_header(citizen),
        json={"ward_id": str(hierarchy["ward"].id)},
    )
    assert response.status_code == 200
    db.refresh(citizen)
    assert citizen.ward_id == hierarchy["ward"].id


def test_admin_cannot_edit_own_admin_record(client, taluka_admin, hierarchy):
    """PUT /admin/admins/{own id} was a self-rescope path.

    _user_scope_filter returns `User.taluka_id == <own taluka>` for a
    taluka_admin — a predicate the caller satisfies — and their own role is in
    the editable list, so the 403 check passed when they targeted themselves.
    """
    response = client.put(
        f"/admin/admins/{taluka_admin.id}",
        headers=auth_header(taluka_admin),
        json={"taluka_id": str(hierarchy["other_taluka"].id)},
    )
    assert response.status_code == 403


def test_admin_cannot_assign_scope_outside_own(client, taluka_admin, hierarchy, db):
    """A new scope must be contained within the assigning admin's own."""
    subordinate = make_user(db, "ward_admin", ward_id=hierarchy["ward"].id, taluka_id=hierarchy["taluka"].id)
    response = client.put(
        f"/admin/admins/{subordinate.id}",
        headers=auth_header(taluka_admin),
        json={"ward_id": str(hierarchy["other_ward"].id)},
    )
    assert response.status_code == 403
    db.refresh(subordinate)
    assert subordinate.ward_id == hierarchy["ward"].id


def test_ward_admin_cannot_delete_user_with_no_scope(client, ward_admin, db):
    """A user with NULL scope columns belongs to no jurisdiction.

    The old guard read `if user.ward_id and user.ward_id != current_user.ward_id`,
    which short-circuits to False when the column is NULL — and NULL is the
    default for every account created via register, verify-otp, Google or
    Aadhaar. So the check silently passed for essentially every citizen.
    """
    unscoped_worker = make_user(db, "worker")  # ward_id/taluka_id/district_id all None
    response = client.delete(
        f"/admin/users/{unscoped_worker.id}", headers=auth_header(ward_admin)
    )
    assert response.status_code == 403
    db.refresh(unscoped_worker)
    assert unscoped_worker.is_active is True


def test_scoped_admin_without_scope_sees_nothing(db):
    """A ward_admin with no ward_id must be denied, not handed the whole system.

    get_admin_scope_filter returned {} for this case — the same value that means
    "super-admin, unrestricted". A fresh database has no wards at all, so this
    was the default state for any admin created before seeding.
    """
    from app.core.deps import DENY_ALL, get_admin_scope_filter

    orphan = make_user(db, "ward_admin", ward_id=None)
    assert get_admin_scope_filter(orphan) == {DENY_ALL: True}


# ── 7.5 — worker task ownership ───────────────────────────────────────────────


def _make_issue(db, reporter, **kwargs):
    from app.models.issue import Issue

    defaults = dict(
        id=uuid.uuid4(),
        reporter_id=reporter.id,
        issue_type="pothole",
        description="test issue",
        latitude=23.02,
        longitude=72.57,
        status="in_progress",
        priority="medium",
        severity="medium",
        before_photos=[],
        after_photos=[],
    )
    defaults.update(kwargs)
    issue = Issue(**defaults)
    db.add(issue)
    db.flush()
    return issue


def test_worker_cannot_resolve_another_workers_issue(
    client, citizen, worker, other_worker, db
):
    """PATCH /issues/{id} had no assigned_worker_id check.

    Unlike POST /workers/tasks/{id}/resolve, which has always filtered on
    ownership. So any worker could close any other worker's task, crediting
    nothing and firing a "resolved" notification at the citizen.
    """
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id)
    response = client.patch(
        f"/issues/{issue.id}",
        headers=auth_header(other_worker),
        json={"status": "resolved"},
    )
    assert response.status_code == 403
    db.refresh(issue)
    assert issue.status == "in_progress"


def test_worker_can_resolve_own_issue(client, citizen, worker, db):
    """The legitimate path still works."""
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id)
    response = client.patch(
        f"/issues/{issue.id}",
        headers=auth_header(worker),
        json={"status": "resolved"},
    )
    assert response.status_code == 200, response.text
    db.refresh(issue)
    assert issue.status == "resolved"


def test_ward_admin_update_is_not_a_silent_noop(client, citizen, ward_admin, hierarchy, db):
    """A sub-admin matched no branch, so PATCH returned 200 having changed nothing."""
    issue = _make_issue(db, citizen, ward_id=hierarchy["ward"].id, status="open")
    response = client.patch(
        f"/issues/{issue.id}",
        headers=auth_header(ward_admin),
        json={"status": "assigned"},
    )
    assert response.status_code == 200, response.text
    db.refresh(issue)
    assert issue.status == "assigned", "ward_admin update silently did nothing"


def test_ward_admin_cannot_touch_issue_outside_scope(
    client, citizen, ward_admin, hierarchy, db
):
    """Routing sub-admins into the admin branch must not widen what they reach."""
    issue = _make_issue(db, citizen, ward_id=hierarchy["other_ward"].id, status="open")
    response = client.patch(
        f"/issues/{issue.id}",
        headers=auth_header(ward_admin),
        json={"status": "assigned"},
    )
    assert response.status_code == 403


# ── 7.6 — /health/integrity ───────────────────────────────────────────────────


def test_health_integrity_requires_admin(client, citizen):
    """Was unauthenticated, rate-limit-exempt, and ran four full table scans."""
    assert client.get("/health/integrity").status_code in (401, 403)
    assert client.get("/health/integrity", headers=auth_header(citizen)).status_code == 403


def test_health_integrity_works_for_admin(client, super_admin):
    response = client.get("/health/integrity", headers=auth_header(super_admin))
    assert response.status_code == 200
    assert response.json()["status"] in ("healthy", "degraded", "unhealthy")


def test_liveness_and_readiness_stay_open(client):
    """The actual probes must not require auth — orchestrators cannot send one."""
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
