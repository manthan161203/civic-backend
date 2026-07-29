"""
``GET /admin/insights`` — aggregates computed in SQL, narrative optional.
========================================================================

There was no insights endpoint, so the console built one in the browser: it
paged ``/admin/issues`` 200 rows at a time up to a 2,000-row ceiling, aggregated
client-side, and rendered an amber banner conceding the figures were "a sample,
not a total". Anyone past 2,000 issues was reading numbers that were wrong by
whatever factor the real dataset happened to be.

What the tests below pin, beyond the counts being right:

* **The narrative is best-effort.** No provider key, or a provider that errors,
  must yield ``narrative: null`` with every number still present. A dashboard
  going blank because a third party is down would be a worse failure than the
  one being fixed.
* **Movement is guarded twice** — by percentage *and* by an absolute base — so
  a category going from 1 issue to 3 is not surfaced as a 200% surge.
* **A category new this period reports ``change_pct: null``**, not a division by
  zero and not "infinite growth".
* **Only aggregates reach the model.** No description, address or reporter id is
  in what gets sent, so the summary cannot leak one however the model behaves.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest

from app.core.time import now_utc
from app.routes.admin import _clear_insight_cache

from tests.conftest import auth_header
from tests.test_phase8_endpoints import _make_issue


@pytest.fixture(autouse=True)
def _no_cached_narratives():
    """The narrative cache is process-local and keyed on scope, not user id.

    Two super-admins deliberately share an entry — they see identical data — but
    that also means one test's cached sentence would be served to the next,
    which silently made a mock look uncalled.
    """
    _clear_insight_cache()
    yield
    _clear_insight_cache()


def _insights(client, user, **params):
    params.setdefault("narrative", "false")
    response = client.get("/admin/insights", params=params, headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()


def _aged(days):
    return now_utc() - timedelta(days=days)


# ── AI quality ────────────────────────────────────────────────────────────────


def test_ai_quality_counts_the_whole_dataset(client, db, citizen, super_admin):
    _make_issue(db, citizen, ai_confidence=0.95)
    _make_issue(db, citizen, ai_confidence=0.42)
    _make_issue(db, citizen, ai_confidence=0.55)
    _make_issue(db, citizen, ai_resolution_quality="poor")
    _make_issue(db, citizen)  # no AI at all

    ai = _insights(client, super_admin)["ai_quality"]
    assert ai["total_issues"] == 5
    assert ai["analysed"] == 4
    assert ai["low_confidence"] == 2
    assert ai["poor_resolutions"] == 1


def test_avg_confidence_ignores_unanalysed_issues(client, db, citizen, super_admin):
    """Averaging nulls as zero would drag the figure down and read as a
    classifier problem rather than a coverage one.
    """
    _make_issue(db, citizen, ai_confidence=0.8)
    _make_issue(db, citizen, ai_confidence=0.6)
    _make_issue(db, citizen)

    assert _insights(client, super_admin)["ai_quality"]["avg_confidence"] == 0.7


def test_avg_confidence_is_null_when_nothing_is_analysed(client, db, citizen, super_admin):
    _make_issue(db, citizen)

    assert _insights(client, super_admin)["ai_quality"]["avg_confidence"] is None


def test_insights_are_scoped(client, db, citizen, ward_admin, other_ward_admin, hierarchy):
    _make_issue(db, citizen, ward_id=hierarchy["ward"].id, ai_confidence=0.1)
    _make_issue(db, citizen, ward_id=hierarchy["other_ward"].id, ai_confidence=0.1)

    assert _insights(client, ward_admin)["ai_quality"]["low_confidence"] == 1
    assert _insights(client, other_ward_admin)["ai_quality"]["low_confidence"] == 1


def test_deleted_issues_are_excluded(client, db, citizen, super_admin):
    _make_issue(db, citizen, ai_confidence=0.1)
    _make_issue(db, citizen, ai_confidence=0.1, is_deleted=True)

    assert _insights(client, super_admin)["ai_quality"]["low_confidence"] == 1


# ── movement ──────────────────────────────────────────────────────────────────


def test_movement_compares_this_period_with_the_last(client, db, citizen, super_admin):
    for _ in range(10):
        _make_issue(db, citizen, issue_type="pothole", created_at=_aged(3))
    for _ in range(4):
        _make_issue(db, citizen, issue_type="pothole", created_at=_aged(40))

    movement = _insights(client, super_admin, days=30)["movement"]
    pothole = next(m for m in movement if m["issue_type"] == "pothole")
    assert pothole["current"] == 10
    assert pothole["previous"] == 4
    assert pothole["delta"] == 6
    assert pothole["change_pct"] == 150.0


def test_a_small_base_is_not_reported_as_notable(client, db, citizen, super_admin):
    """1 → 3 is a 200% change and completely uninteresting.

    Without the absolute-base guard the dashboard fills with noise that looks
    like findings.
    """
    _make_issue(db, citizen, issue_type="garbage", created_at=_aged(40))
    for _ in range(3):
        _make_issue(db, citizen, issue_type="garbage", created_at=_aged(3))

    movement = _insights(client, super_admin, days=30)["movement"]
    garbage = next(m for m in movement if m["issue_type"] == "garbage")
    assert garbage["change_pct"] == 200.0
    assert garbage["notable"] is False, "3 issues is not a surge"


def test_a_large_base_moving_sharply_is_notable(client, db, citizen, super_admin):
    for _ in range(10):
        _make_issue(db, citizen, issue_type="streetlight", created_at=_aged(40))
    for _ in range(20):
        _make_issue(db, citizen, issue_type="streetlight", created_at=_aged(3))

    movement = _insights(client, super_admin, days=30)["movement"]
    streetlight = next(m for m in movement if m["issue_type"] == "streetlight")
    assert streetlight["notable"] is True


def test_a_brand_new_category_reports_null_rather_than_dividing_by_zero(
    client, db, citizen, super_admin
):
    for _ in range(5):
        _make_issue(db, citizen, issue_type="drain", created_at=_aged(3))

    movement = _insights(client, super_admin, days=30)["movement"]
    drain = next(m for m in movement if m["issue_type"] == "drain")
    assert drain["previous"] == 0
    assert drain["change_pct"] is None, "no baseline means no percentage"
    assert drain["delta"] == 5
    assert drain["notable"] is False


def test_a_category_that_vanished_still_appears(client, db, citizen, super_admin):
    """A type dropping to zero is a finding, not an absence."""
    for _ in range(8):
        _make_issue(db, citizen, issue_type="garbage", created_at=_aged(40))

    movement = _insights(client, super_admin, days=30)["movement"]
    garbage = next(m for m in movement if m["issue_type"] == "garbage")
    assert (garbage["current"], garbage["previous"]) == (0, 8)
    assert garbage["change_pct"] == -100.0
    assert garbage["notable"] is True


# ── throughput ────────────────────────────────────────────────────────────────


def test_backlog_ratio_above_one_means_the_backlog_grew(client, db, citizen, super_admin):
    for _ in range(6):
        _make_issue(db, citizen, created_at=_aged(3))
    _make_issue(db, citizen, created_at=_aged(3), status="resolved", resolved_at=_aged(2))
    _make_issue(db, citizen, created_at=_aged(3), status="resolved", resolved_at=_aged(2))

    throughput = _insights(client, super_admin, days=30)["throughput"]
    assert throughput["opened"] == 8
    assert throughput["resolved"] == 2
    assert throughput["backlog_ratio"] == 4.0


def test_backlog_ratio_is_null_when_nothing_was_resolved(client, db, citizen, super_admin):
    """Not `inf`, and not a crash."""
    _make_issue(db, citizen, created_at=_aged(3))

    assert _insights(client, super_admin, days=30)["throughput"]["backlog_ratio"] is None


# ── the narrative degrades, it does not break ─────────────────────────────────


def test_no_provider_key_yields_a_null_narrative_and_full_numbers(
    client, db, citizen, super_admin
):
    _make_issue(db, citizen, ai_confidence=0.2)

    response = client.get(
        "/admin/insights", params={"narrative": "true"}, headers=auth_header(super_admin)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["narrative"] is None
    assert body["ai_quality"]["low_confidence"] == 1, "numbers survive without it"


def test_a_failing_provider_does_not_fail_the_request(client, db, citizen, super_admin):
    """The dashboard must degrade to numbers, not to a 500."""
    _make_issue(db, citizen, ai_confidence=0.2)

    with patch("app.core.config.settings.GROQ_API_KEY", "test-key"), patch(
        "app.services.ai_service.summarise_insights", side_effect=RuntimeError("provider down")
    ):
        response = client.get(
            "/admin/insights", params={"narrative": "true"}, headers=auth_header(super_admin)
        )

    assert response.status_code == 200, response.text
    assert response.json()["narrative"] is None
    assert response.json()["ai_quality"]["low_confidence"] == 1


def test_the_narrative_is_returned_when_the_provider_answers(
    client, db, citizen, super_admin
):
    _make_issue(db, citizen, ai_confidence=0.2)

    with patch("app.core.config.settings.GROQ_API_KEY", "test-key"), patch(
        "app.services.ai_service.summarise_insights", return_value="Pothole reports doubled."
    ):
        response = client.get(
            "/admin/insights", params={"narrative": "true"}, headers=auth_header(super_admin)
        )

    assert response.json()["narrative"] == "Pothole reports doubled."


def test_the_narrative_is_cached_between_requests(client, db, citizen, super_admin):
    """Otherwise every dashboard load is an LLM round-trip."""
    _make_issue(db, citizen, ai_confidence=0.2)
    calls = []

    def _count(payload):
        calls.append(payload)
        return "cached summary"

    with patch("app.core.config.settings.GROQ_API_KEY", "test-key"), patch(
        "app.services.ai_service.summarise_insights", side_effect=_count
    ):
        first = client.get(
            "/admin/insights", params={"narrative": "true"}, headers=auth_header(super_admin)
        )
        second = client.get(
            "/admin/insights", params={"narrative": "true"}, headers=auth_header(super_admin)
        )

    assert len(calls) == 1, "the second request must be served from the cache"
    assert second.json()["narrative"] == "cached summary"
    # Dated, so a reader can tell how old the sentence is relative to the
    # numbers beside it — which are recomputed on every request.
    assert first.json()["narrative_generated_at"] is not None
    assert second.json()["narrative_generated_at"] == first.json()["narrative_generated_at"]


def test_a_different_period_is_cached_separately(client, db, citizen, super_admin):
    """7-day and 30-day figures are different data and need different prose."""
    _make_issue(db, citizen, ai_confidence=0.2)
    calls = []

    with patch("app.core.config.settings.GROQ_API_KEY", "test-key"), patch(
        "app.services.ai_service.summarise_insights",
        side_effect=lambda p: calls.append(p) or "summary",
    ):
        client.get(
            "/admin/insights", params={"narrative": "true", "days": 7},
            headers=auth_header(super_admin),
        )
        client.get(
            "/admin/insights", params={"narrative": "true", "days": 30},
            headers=auth_header(super_admin),
        )

    assert len(calls) == 2


def test_narrative_false_skips_the_provider_entirely(client, db, citizen, super_admin):
    _make_issue(db, citizen, ai_confidence=0.2)
    calls = []

    with patch("app.core.config.settings.GROQ_API_KEY", "test-key"), patch(
        "app.services.ai_service.summarise_insights",
        side_effect=lambda p: calls.append(p) or "summary",
    ):
        response = client.get(
            "/admin/insights", params={"narrative": "false"}, headers=auth_header(super_admin)
        )

    assert calls == []
    assert response.json()["narrative"] is None


def test_only_aggregates_are_sent_to_the_model(client, db, citizen, super_admin):
    """No description, address or reporter id may reach a third party."""
    _make_issue(
        db, citizen,
        description="Ravi Patel, flat 12B, mobile 9876543210",
        address="12B Nehru Road",
        ai_confidence=0.2,
    )

    captured = {}

    def _capture(payload):
        captured["payload"] = payload
        return "summary"

    with patch("app.core.config.settings.GROQ_API_KEY", "test-key"), patch(
        "app.services.ai_service.summarise_insights", side_effect=_capture
    ):
        client.get(
            "/admin/insights", params={"narrative": "true"}, headers=auth_header(super_admin)
        )

    serialised = str(captured["payload"])
    assert "Ravi Patel" not in serialised
    assert "9876543210" not in serialised
    assert "Nehru Road" not in serialised


# ── the drill-down filter that replaces the client-side scan ──────────────────


def test_ai_flag_filters_to_low_confidence(client, db, citizen, super_admin):
    _make_issue(db, citizen, ai_confidence=0.2)
    _make_issue(db, citizen, ai_confidence=0.9)
    _make_issue(db, citizen)

    response = client.get(
        "/admin/issues", params={"ai_flag": "low_confidence"}, headers=auth_header(super_admin)
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_ai_flag_filters_to_poor_resolutions(client, db, citizen, super_admin):
    _make_issue(db, citizen, ai_resolution_quality="poor")
    _make_issue(db, citizen, ai_resolution_quality="good")

    response = client.get(
        "/admin/issues", params={"ai_flag": "poor_resolution"}, headers=auth_header(super_admin)
    )
    assert response.json()["total"] == 1


def test_an_unknown_ai_flag_is_rejected_not_ignored(client, db, citizen, super_admin):
    """Ignoring it would show every issue under a 'low confidence' tab."""
    _make_issue(db, citizen)

    response = client.get(
        "/admin/issues", params={"ai_flag": "nonsense"}, headers=auth_header(super_admin)
    )
    assert response.status_code == 422, response.text


# ── access ────────────────────────────────────────────────────────────────────


def test_a_citizen_cannot_read_insights(client, citizen):
    response = client.get("/admin/insights", headers=auth_header(citizen))
    assert response.status_code == 403
