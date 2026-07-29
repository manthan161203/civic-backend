"""`GET /admin/issues` — status is an enum, escalation and blocking are not.

The console offered "escalated" in its status dropdown. `Issue.status` is a
native Postgres enum of exactly five values, so that did not return an empty
list — psycopg raised

    invalid input value for enum issue_status: "escalated"

which the handler's `except Exception` converted into
``500 "Failed to fetch issues."`` Selecting a filter option crashed the request.

Underneath the 500 was a real gap: escalation and blocking are boolean columns
sitting *alongside* status (an issue can be `in_progress` and escalated), and
there was no way to filter on either. So the most operationally urgent slice of
the queue could not be listed at all.

These tests pin both halves: the guard that turns a bad status into a 422, and
the filters that make the question answerable.
"""

import uuid

from app.models.issue import Issue
from tests.conftest import auth_header


def _issue(db, *, ward, reporter, status="open", escalated=False, blocked=False, desc="x"):
    issue = Issue(
        id=uuid.uuid4(),
        reporter_id=reporter.id,
        issue_type="pothole",
        description=desc,
        latitude=22.3,
        longitude=71.2,
        status=status,
        ward_id=ward.id,
        is_escalated=escalated,
        is_blocked=blocked,
    )
    db.add(issue)
    db.flush()
    return issue


def _descs(payload):
    return {i["description"] for i in payload["items"]}


class TestStatusValidation:
    def test_escalated_as_a_status_is_422_not_500(self, client, db, hierarchy, ward_admin):
        """The exact request the console used to send.

        422 rather than 500 matters beyond the status code: a 500 is a bug
        report, a 422 tells the caller what to send instead.
        """
        r = client.get(
            "/admin/issues", params={"status": "escalated"}, headers=auth_header(ward_admin)
        )

        assert r.status_code == 422
        detail = r.json()["detail"]
        # Names the alternative, so the fix is in the error rather than in a
        # changelog somebody has to find.
        assert "is_escalated" in detail

    def test_blocked_as_a_status_is_also_rejected(self, client, db, hierarchy, ward_admin):
        r = client.get(
            "/admin/issues", params={"status": "blocked"}, headers=auth_header(ward_admin)
        )
        assert r.status_code == 422

    def test_every_real_status_is_accepted(self, client, db, hierarchy, ward_admin):
        for status in ("open", "assigned", "in_progress", "resolved", "closed"):
            r = client.get(
                "/admin/issues", params={"status": status}, headers=auth_header(ward_admin)
            )
            assert r.status_code == 200, f"{status} was rejected"

    def test_omitting_status_is_unfiltered(self, client, db, hierarchy, ward_admin, citizen):
        _issue(db, ward=hierarchy["ward"], reporter=citizen, status="open", desc="a")
        _issue(db, ward=hierarchy["ward"], reporter=citizen, status="resolved", desc="b")
        db.commit()

        r = client.get("/admin/issues", headers=auth_header(ward_admin))

        assert r.status_code == 200
        assert _descs(r.json()) == {"a", "b"}


class TestEscalatedFilter:
    def test_selects_only_escalated(self, client, db, hierarchy, ward_admin, citizen):
        ward = hierarchy["ward"]
        _issue(db, ward=ward, reporter=citizen, escalated=True, desc="escalated")
        _issue(db, ward=ward, reporter=citizen, escalated=False, desc="normal")
        db.commit()

        r = client.get(
            "/admin/issues", params={"is_escalated": True}, headers=auth_header(ward_admin)
        )

        assert r.status_code == 200
        assert _descs(r.json()) == {"escalated"}

    def test_false_selects_the_rest(self, client, db, hierarchy, ward_admin, citizen):
        ward = hierarchy["ward"]
        _issue(db, ward=ward, reporter=citizen, escalated=True, desc="escalated")
        _issue(db, ward=ward, reporter=citizen, escalated=False, desc="normal")
        db.commit()

        r = client.get(
            "/admin/issues", params={"is_escalated": False}, headers=auth_header(ward_admin)
        )

        assert r.status_code == 200
        assert _descs(r.json()) == {"normal"}

    def test_composes_with_status_rather_than_replacing_it(
        self, client, db, hierarchy, ward_admin, citizen
    ):
        """The reason these are separate parameters.

        An issue is escalated *and* has a status. Treating escalation as a
        status made the two mutually exclusive, so "escalated work in progress"
        — the thing an admin most wants to see — was unaskable.
        """
        ward = hierarchy["ward"]
        _issue(db, ward=ward, reporter=citizen, status="in_progress", escalated=True, desc="both")
        _issue(db, ward=ward, reporter=citizen, status="open", escalated=True, desc="escalated-open")
        _issue(db, ward=ward, reporter=citizen, status="in_progress", desc="just-progress")
        db.commit()

        r = client.get(
            "/admin/issues",
            params={"is_escalated": True, "status": "in_progress"},
            headers=auth_header(ward_admin),
        )

        assert r.status_code == 200
        assert _descs(r.json()) == {"both"}

    def test_respects_jurisdiction(self, client, db, hierarchy, ward_admin, citizen):
        """Filters compose with scoping rather than bypassing it."""
        _issue(db, ward=hierarchy["ward"], reporter=citizen, escalated=True, desc="mine")
        _issue(db, ward=hierarchy["other_ward"], reporter=citizen, escalated=True, desc="theirs")
        db.commit()

        r = client.get(
            "/admin/issues", params={"is_escalated": True}, headers=auth_header(ward_admin)
        )

        assert r.status_code == 200
        assert _descs(r.json()) == {"mine"}


class TestBlockedFilter:
    def test_selects_only_blocked(self, client, db, hierarchy, ward_admin, citizen):
        ward = hierarchy["ward"]
        _issue(db, ward=ward, reporter=citizen, blocked=True, desc="blocked")
        _issue(db, ward=ward, reporter=citizen, blocked=False, desc="fine")
        db.commit()

        r = client.get(
            "/admin/issues", params={"is_blocked": True}, headers=auth_header(ward_admin)
        )

        assert r.status_code == 200
        assert _descs(r.json()) == {"blocked"}

    def test_blocked_and_escalated_are_independent(
        self, client, db, hierarchy, ward_admin, citizen
    ):
        ward = hierarchy["ward"]
        _issue(db, ward=ward, reporter=citizen, blocked=True, escalated=False, desc="blocked-only")
        _issue(db, ward=ward, reporter=citizen, blocked=False, escalated=True, desc="escalated-only")
        _issue(db, ward=ward, reporter=citizen, blocked=True, escalated=True, desc="both")
        db.commit()

        r = client.get(
            "/admin/issues",
            params={"is_blocked": True, "is_escalated": True},
            headers=auth_header(ward_admin),
        )

        assert r.status_code == 200
        assert _descs(r.json()) == {"both"}


class TestCitizensCannotUseThis:
    def test_citizen_is_refused(self, client, db, citizen):
        r = client.get(
            "/admin/issues", params={"is_escalated": True}, headers=auth_header(citizen)
        )
        assert r.status_code == 403
