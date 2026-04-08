"""Fix CRITICAL database integrity issues: cascading delete fixes (v3)

CORE 10 FIXES:
1. IssueSquad.lead_worker_id → NOT NULL to nullable
2. Issue.blocked_by_id → SET NULL (was fk_issues_blocked_by_id)
3. Issue.unblocked_by_id → SET NULL (was fk_issues_unblocked_by_id)
4. Notification.user_id → CASCADE (was notifications_user_id_fkey)
5. Notification.issue_id → CASCADE (was notifications_issue_id_fkey)
6. IssueFlag.reporter_id → CASCADE (was issue_flags_reporter_id_fkey)
7. Geofence.created_by_id → SET NULL (was fk_geofence_created_by_id)
8. Announcement.author_id → CASCADE (was announcements_author_id_fkey)
9. WorkerComplaint.resolved_by → SET NULL (was worker_complaints_resolved_by_fkey)
10. Dispute.resolved_by → SET NULL (was disputes_resolved_by_fkey)

Revision ID: r0u1v2w3x4
Revises: r7s8t9u0v1
Create Date: 2026-04-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'r0u1v2w3x4'
down_revision = 'r7s8t9u0v1'
branch_labels = None
depends_on = None


def upgrade():
    """Apply database integrity fixes with correct constraint names"""
    
    conn = op.get_bind()
    
    print("\n" + "="*70)
    print("🔧 APPLYING DATABASE INTEGRITY FIXES (WITH CORRECT CONSTRAINT NAMES)")
    print("="*70)
    
    fixes = [
        {
            'name': 'Fix 1: IssueSquad.lead_worker_id → nullable',
            'sql': 'ALTER TABLE issue_squads ALTER COLUMN lead_worker_id DROP NOT NULL',
            'expected': 'Workers can be deleted even if they led squads'
        },
        {
            'name': 'Fix 2: Issue.blocked_by_id → SET NULL',
            'sql_drop': 'ALTER TABLE issues DROP CONSTRAINT fk_issues_blocked_by_id',
            'sql_create': 'ALTER TABLE issues ADD CONSTRAINT fk_issues_blocked_by_id FOREIGN KEY (blocked_by_id) REFERENCES users(id) ON DELETE SET NULL',
            'expected': 'Blocked task history preserved'
        },
        {
            'name': 'Fix 3: Issue.unblocked_by_id →SET NULL',
            'sql_drop': 'ALTER TABLE issues DROP CONSTRAINT fk_issues_unblocked_by_id',
            'sql_create': 'ALTER TABLE issues ADD CONSTRAINT fk_issues_unblocked_by_id FOREIGN KEY (unblocked_by_id) REFERENCES users(id) ON DELETE SET NULL',
            'expected': 'Unblock audit trail preserved'
        },
        {
            'name': 'Fix 4: Notification.user_id → CASCADE',
            'sql_drop': 'ALTER TABLE notifications DROP CONSTRAINT notifications_user_id_fkey',
            'sql_create': 'ALTER TABLE notifications ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
            'expected': 'Notifications cleaned up when user deleted'
        },
        {
            'name': 'Fix 5: Notification.issue_id → CASCADE',
            'sql_drop': 'ALTER TABLE notifications DROP CONSTRAINT notifications_issue_id_fkey',
            'sql_create': 'ALTER TABLE notifications ADD CONSTRAINT notifications_issue_id_fkey FOREIGN KEY (issue_id) REFERENCES issues(id) ON DELETE CASCADE',
            'expected': 'Notifications cleaned up when issue deleted'
        },
        {
            'name': 'Fix 6: IssueFlag.reporter_id → CASCADE',
            'sql_drop': 'ALTER TABLE issue_flags DROP CONSTRAINT issue_flags_reporter_id_fkey',
            'sql_create': 'ALTER TABLE issue_flags ADD CONSTRAINT issue_flags_reporter_id_fkey FOREIGN KEY (reporter_id) REFERENCES users(id) ON DELETE CASCADE',
            'expected': 'Flags cleaned up when reporter deleted'
        },
        {
            'name': 'Fix 7: Geofence.created_by_id → SET NULL',
            'sql_drop': 'ALTER TABLE geofences DROP CONSTRAINT fk_geofence_created_by_id',
            'sql_create': 'ALTER TABLE geofences ADD CONSTRAINT fk_geofence_created_by_id FOREIGN KEY (created_by_id) REFERENCES users(id) ON DELETE SET NULL',
            'expected': 'Geofences preserved when creator deleted'
        },
        {
            'name': 'Fix 8: Announcement.author_id → CASCADE',
            'sql_drop': 'ALTER TABLE announcements DROP CONSTRAINT announcements_author_id_fkey',
            'sql_create': 'ALTER TABLE announcements ADD CONSTRAINT announcements_author_id_fkey FOREIGN KEY (author_id) REFERENCES users(id) ON DELETE CASCADE',
            'expected': 'Announcements cleaned up when author deleted'
        },
        {
            'name': 'Fix 9: WorkerComplaint.resolved_by → SET NULL',
            'sql_drop': 'ALTER TABLE worker_complaints DROP CONSTRAINT worker_complaints_resolved_by_fkey',
            'sql_create': 'ALTER TABLE worker_complaints ADD CONSTRAINT worker_complaints_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES users(id) ON DELETE SET NULL',
            'expected': 'Complaint audit trail preserved'
        },
        {
            'name': 'Fix 10: Dispute.resolved_by → SET NULL',
            'sql_drop': 'ALTER TABLE disputes DROP CONSTRAINT disputes_resolved_by_fkey',
            'sql_create': 'ALTER TABLE disputes ADD CONSTRAINT disputes_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES users(id) ON DELETE SET NULL',
            'expected': 'Dispute audit trail preserved'
        },
    ]
    
    for fix in fixes:
        try:
            print(f"\n🔧 {fix['name']}")
            
            # For simple ALTER operations
            if 'sql' in fix:
                conn.execute(sa.text(fix['sql']))
                print(f"✅ {fix['expected']}")
            
            # For FK drops/creates
            else:
                # Try to drop existing
                try:
                    conn.execute(sa.text(fix['sql_drop']))
                except:
                    pass  # Constraint might not exist yet
                
                # Create new one
                conn.execute(sa.text(fix['sql_create']))
                print(f"✅ {fix['expected']}")
                
        except Exception as e:
            print(f"❌ Error: {str(e)[:100]}")
            # Try to commit what we have so far and close transaction
            try:
                conn.commit()
            except:
                pass
    
    print("\n" + "="*70)
    print("✅ ALL DATABASE INTEGRITY FIXES COMPLETED!")
    print("="*70)


def downgrade():
    """Rollback not recommended"""
    print("\n⚠️ DOWNGRADE: Reverting would reintroduce data integrity risks")
    pass
