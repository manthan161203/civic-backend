"""
AI scoping regression tests (Phase 7.7).
========================================

The chat assistant used to be handed LangChain's ``QuerySQLDatabaseTool`` bound
to the application's read-write connection. The model wrote SQL, the tool ran it,
and the only restriction was prose in the system prompt. ``otps.code`` (a live
login credential), ``users.password_hash`` and ``users.aadhar_hash`` were all
reachable by a citizen who typed "ignore previous instructions".

These tests assert the structural property that replaced it: the model has no
way to express a query, and the tools it does have cannot see outside the
caller's scope. They do not call an LLM — the guarantee is in the tool layer, so
that is what is tested.
"""

import uuid


from app.services import ai_service
from app.services.ai_tools import build_tools

from tests.conftest import make_user


def _make_issue(db, reporter, **kwargs):
    from app.models.issue import Issue

    defaults = dict(
        id=uuid.uuid4(),
        reporter_id=reporter.id,
        issue_type="pothole",
        description="test issue",
        latitude=23.02,
        longitude=72.57,
        status="open",
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


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


# ── The SQL surface is gone ───────────────────────────────────────────────────


def test_no_sql_execution_tool_is_exposed(db, citizen):
    """No tool may accept a query string.

    This is the structural guarantee. If a tool ever takes free-form SQL again,
    prompt injection becomes arbitrary database access and this test fails.
    """
    tools = build_tools(db, citizen)
    for t in tools:
        schema = t.args_schema.model_json_schema() if t.args_schema else {"properties": {}}
        for arg in schema.get("properties", {}):
            assert "query" not in arg.lower(), f"tool {t.name} accepts {arg!r}"
            assert "sql" not in arg.lower(), f"tool {t.name} accepts {arg!r}"


def test_dead_sql_helpers_are_removed():
    """The old blocklist helpers were never wired in — they looked like guards.

    Keeping them around invites someone to believe the agent is protected.
    """
    for name in ("sql_db_query", "sql_db_query_checker", "sql_db_schema", "sql_db_list_tables", "execute_sql_agent"):
        assert not hasattr(ai_service, name), f"{name} is still present"


def test_agent_is_gated_on_groq_not_gemini(db, citizen, monkeypatch):
    """build_dynamic_context checked the *Gemini* client while the agent uses Groq.

    So configuring only GROQ_API_KEY — the documented key for chat and the
    agent — silently disabled the feature entirely.
    """
    monkeypatch.setattr(ai_service.settings, "GROQ_API_KEY", "", raising=False)
    monkeypatch.setattr(ai_service.settings, "GEMINI_API_KEY", "set-but-irrelevant", raising=False)
    assert ai_service.build_dynamic_context("how many issues?", citizen, db, "base") == "base"


# ── Tools are scoped to the caller ────────────────────────────────────────────


def test_citizen_tool_sees_only_own_issues(db, citizen, other_citizen):
    mine = _make_issue(db, citizen, description="mine")
    _make_issue(db, other_citizen, description="theirs")

    out = _tool(build_tools(db, citizen), "list_issues").invoke({})
    assert str(mine.id)[:8] in out
    assert "theirs" not in out


def test_citizen_counts_exclude_other_peoples_issues(db, citizen, other_citizen):
    _make_issue(db, citizen)
    _make_issue(db, other_citizen)
    _make_issue(db, other_citizen)

    out = _tool(build_tools(db, citizen), "issue_counts_by_status").invoke({})
    assert "open: 1" in out


def test_issue_detail_cannot_reach_another_users_issue(db, citizen, other_citizen):
    """Even given the exact ID, the scoped query must not return it."""
    theirs = _make_issue(db, other_citizen)
    out = _tool(build_tools(db, citizen), "issue_detail").invoke(
        {"issue_id_prefix": str(theirs.id)}
    )
    assert "outside what you can access" in out


def test_worker_sees_assigned_and_reported_only(db, citizen, worker, other_worker):
    assigned = _make_issue(db, citizen, assigned_worker_id=worker.id)
    _make_issue(db, citizen, assigned_worker_id=other_worker.id, description="not mine")

    out = _tool(build_tools(db, worker), "list_issues").invoke({})
    assert str(assigned.id)[:8] in out
    assert "not mine" not in out


def test_ward_admin_tool_respects_geographic_scope(db, citizen, ward_admin, hierarchy):
    inside = _make_issue(db, citizen, ward_id=hierarchy["ward"].id)
    outside = _make_issue(db, citizen, ward_id=hierarchy["other_ward"].id)

    out = _tool(build_tools(db, ward_admin), "list_issues").invoke({})
    assert str(inside.id)[:8] in out
    assert str(outside.id)[:8] not in out


def test_unscoped_admin_sees_nothing(db, citizen):
    """A ward_admin with no ward assigned must be denied, not shown everything."""
    _make_issue(db, citizen)
    orphan = make_user(db, "ward_admin", ward_id=None)
    out = _tool(build_tools(db, orphan), "list_issues").invoke({})
    assert out == "No matching issues."


def test_rewards_tool_is_self_only(db, citizen, other_citizen):
    """my_rewards takes no user argument — the model cannot ask about someone else."""
    tools = build_tools(db, citizen)
    schema = _tool(tools, "my_rewards").args_schema
    props = schema.model_json_schema().get("properties", {}) if schema else {}
    assert "user_id" not in props


def test_team_workload_refuses_non_admins(db, citizen):
    out = _tool(build_tools(db, citizen), "team_workload").invoke({})
    assert "administrators only" in out


def test_row_limit_is_enforced_regardless_of_request(db, citizen):
    """A tool must cap its own output even if the model asks for more."""
    from app.services.ai_tools import MAX_ROWS

    for _ in range(MAX_ROWS + 5):
        _make_issue(db, citizen)

    out = _tool(build_tools(db, citizen), "list_issues").invoke({"limit": 10_000})
    assert len(out.strip().split("\n")) <= MAX_ROWS


def test_invalid_status_is_rejected_not_interpolated(db, citizen):
    out = _tool(build_tools(db, citizen), "list_issues").invoke(
        {"status": "'; DROP TABLE users; --"}
    )
    assert "Unknown status" in out
