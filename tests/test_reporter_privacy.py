"""
The reporter's phone number is not public.
=========================================

``IssueReporterInfo`` carried ``phone``, and ``IssueResponse`` is
``from_attributes`` over a model with a ``reporter`` relationship — so *every*
``IssueResponse.model_validate(issue)`` populated it, at 24 call sites. The
exposure was never limited to admins:

* ``GET /issues/nearby`` takes only ``Depends(get_current_user)`` and returns a
  **list**. Any signed-in citizen got the phone number of everyone who had
  reported an issue near a chosen coordinate — no UUID guessing needed, and the
  coordinate is attacker-supplied.
* ``GET /issues/{id}``'s own docstring says "Citizens can view any issue"; the
  single access check there restricts *workers*, not citizens.

The fix is structural rather than a filter at each of the 24 sites: ``phone`` is
gone from the base schema, so no ``model_validate`` can put it on the wire. An
admin variant re-adds it and the admin routes opt in explicitly. A new endpoint
that forgets to opt in leaks nothing, which is the property a per-site strip
could not give.
"""

from tests.conftest import auth_header
from tests.test_phase8_endpoints import _make_issue

REPORTER_PHONE = "+919812345678"


def _reporter(db):
    """A citizen whose phone we can look for in responses."""
    from tests.conftest import make_user

    return make_user(db, "citizen", phone=REPORTER_PHONE, name="Reporter Rita")


# ── citizen-facing endpoints must not carry the phone ────────────────────────


def test_nearby_does_not_leak_reporter_phones(client, db, other_citizen):
    """The bulk case, and the worst one: a list keyed on a chosen coordinate."""
    reporter = _reporter(db)
    _make_issue(db, reporter, latitude=23.02, longitude=72.57)

    response = client.get(
        "/issues/nearby",
        params={"lat": 23.02, "lng": 72.57, "radius_km": 5},
        headers=auth_header(other_citizen),
    )
    assert response.status_code == 200, response.text
    assert REPORTER_PHONE not in response.text


def test_issue_detail_does_not_leak_reporter_phone(client, db, other_citizen):
    """Any citizen may read any issue, so the detail route is equally exposed."""
    reporter = _reporter(db)
    issue = _make_issue(db, reporter)

    response = client.get(f"/issues/{issue.id}", headers=auth_header(other_citizen))
    assert response.status_code == 200, response.text
    assert REPORTER_PHONE not in response.text
    # The reporter is still identified — only the phone is withheld.
    assert response.json()["reporter"]["name"] == "Reporter Rita"


def test_worker_task_list_does_not_leak_reporter_phone(client, db, worker):
    reporter = _reporter(db)
    _make_issue(db, reporter, assigned_worker_id=worker.id, status="assigned")

    response = client.get("/workers/tasks", headers=auth_header(worker))
    assert response.status_code == 200, response.text
    assert REPORTER_PHONE not in response.text


def test_base_schema_cannot_carry_a_phone():
    """The guarantee is structural, not a filter a new route could forget.

    If someone re-adds ``phone`` to ``IssueReporterInfo``, this fails here rather
    than silently on whichever endpoint is written next.
    """
    from app.schemas.issue import (
        IssueReporterAdminInfo,
        IssueReporterInfo,
        IssueResponse,
    )

    assert "phone" not in IssueReporterInfo.model_fields
    assert "phone" in IssueReporterAdminInfo.model_fields

    annotation = IssueResponse.model_fields["reporter"].annotation
    assert IssueReporterAdminInfo not in getattr(annotation, "__args__", ()), (
        "IssueResponse.reporter must be the narrow type; the admin variant "
        "belongs on IssueAdminResponse only"
    )


# ── admins still get it ───────────────────────────────────────────────────────


def test_admin_issue_list_still_carries_the_phone(client, db, super_admin):
    """Admins call citizens back about their reports; that use is legitimate."""
    reporter = _reporter(db)
    _make_issue(db, reporter)

    response = client.get("/admin/issues", headers=auth_header(super_admin))
    assert response.status_code == 200, response.text
    assert REPORTER_PHONE in response.text


def test_admin_single_issue_response_carries_the_phone(client, db, super_admin):
    """A single-object admin route, not just the list — they are separate
    ``response_model`` declarations and could diverge.
    """
    reporter = _reporter(db)
    issue = _make_issue(db, reporter)

    response = client.post(
        f"/admin/issues/{issue.id}/escalate", headers=auth_header(super_admin)
    )
    assert response.status_code == 200, response.text
    assert response.json()["reporter"]["phone"] == REPORTER_PHONE


def test_admin_variant_is_a_superset_of_the_public_one(db):
    """Guards against the admin schema drifting into a different shape."""
    from app.schemas.issue import IssueAdminResponse, IssueResponse

    missing = set(IssueResponse.model_fields) - set(IssueAdminResponse.model_fields)
    assert not missing, f"admin response dropped public fields: {sorted(missing)}"


def test_admin_list_pagination_survives_the_subclass(client, db, super_admin):
    """`IssueAdminListResponse` overrides `items`, and `IssueListResponse` carries
    a `mode="before"` validator that reconciles page/size with limit/offset.
    Subclassing must not detach it — a silent regression would put every admin
    list back to reporting `limit: 50, offset: 0` on every page.
    """
    reporter = _reporter(db)
    for _ in range(3):
        _make_issue(db, reporter)

    response = client.get(
        "/admin/issues", params={"page": 2, "size": 2}, headers=auth_header(super_admin)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page"] == 2
    assert body["size"] == 2
    assert body["offset"] == 2, "offset must be derived from page/size, not defaulted"
