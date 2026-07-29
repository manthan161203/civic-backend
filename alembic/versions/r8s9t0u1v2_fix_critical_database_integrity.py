"""Fix CRITICAL database integrity issues: cascading delete & FK constraints

Revision ID: r8s9t0u1v2
Revises: r7s8t9u0v1
Create Date: 2026-04-08 10:00:00.000000

COMPREHENSIVE DATABASE INTEGRITY AUDIT & FIXES:
When a worker/user is deleted, here's what should happen:

WORKER DELETION CASCADE:
├─ WorkerShift → CASCADE DELETE ✅ (already correct)
├─ WorkerComplaint (as worker) → CASCADE DELETE ✅ (already correct) 
├─ Issue.assigned_worker → SET NULL (already correct in model)
├─ Notification (if user owns them) → CASCADE DELETE ❌ MISSING - FIXED
├─ RewardTransaction → CASCADE DELETE ✅ (already correct)
├─ UserBadge → CASCADE DELETE ✅ (already correct)
├─ RefreshToken → CASCADE DELETE ✅ (already correct)
├─ Issue.blocked_by_id → SET NULL ❌ MISSING - FIXED
├─ Issue.unblocked_by_id → SET NULL ❌ MISSING - FIXED
├─ IssueSquad.lead_worker (lead_worker_id NOT NULL mismatch) ❌ FIXED
├─ IssueFlag (as reporter) → CASCADE DELETE ❌ MISSING - FIXED
├─ Geofence (as creator) → SET NULL ❌ MISSING - FIXED
├─ Announcement (as author) → CASCADE DELETE ❌ MISSING - FIXED
├─ WorkerComplaint (as resolver) → SET NULL ❌ MISSING - FIXED
└─ Dispute (as resolver) → SET NULL ❌ MISSING - FIXED

FIXES IN THIS MIGRATION:
1. ✅ IssueSquad.lead_worker_id: Make nullable (constraint mismatch fix)
2. ✅ Issue.blocked_by_id: Add ondelete="SET NULL"
3. ✅ Issue.unblocked_by_id: Add ondelete="SET NULL"
4. ✅ Notification.user_id: Add ondelete="CASCADE"
5. ✅ Notification.issue_id: Add ondelete="CASCADE"
6. ✅ IssueFlag.reporter_id: Add ondelete="CASCADE"
7. ✅ Geofence.created_by_id: Add ondelete="SET NULL"
8. ✅ Announcement.author_id: Add ondelete="CASCADE"
9. ✅ WorkerComplaint.resolved_by: Add ondelete="SET NULL"
10. ✅ Dispute.resolved_by: Add ondelete="SET NULL"

SUPERSEDED — this revision's DDL is now a no-op.
==============================================
Three revisions were authored against the same parent (``r7s8t9u0v1``), each
applying this same set of ten fixes: this one, ``r9t0u1v2w3``, and
``r0u1v2w3x4`` (v3). ``merge_integrity_heads`` rejoins them, so all three run
on every database, and re-applying the same DDL a second and third time either
fails outright or leaves duplicate foreign keys behind.

v3 is the canonical implementation and uses the constraint names that actually
exist, so the fixes are applied there. This file is kept — rather than deleted —
so that databases which already recorded this revision keep a valid history.
"""

from alembic import op
import sqlalchemy as sa

revision = 'r8s9t0u1v2'
down_revision = 'r7s8t9u0v1'
branch_labels = None
depends_on = None


def upgrade():
    """No-op. Superseded by revision ``r0u1v2w3x4`` (v3).

    See the module docstring above for the history. Nothing is executed here.
    """
    pass


def downgrade():
    """No-op. See ``upgrade``."""
    pass
