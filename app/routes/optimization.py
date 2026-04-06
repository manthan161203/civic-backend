"""
Stream 7: Backend Optimization - Query Optimization & Performance Improvements
FastAPI endpoints with database query optimization, caching, and new features
"""

from fastapi import APIRouter, Depends, Query, BackgroundTasks
from sqlalchemy import func, and_, or_
from sqlalchemy.orm import Session, selectinload
from datetime import datetime, timedelta
import hashlib

from app.database import get_db
from app.models.issue import Issue
from app.models.user import User
from app.core.deps import get_current_user

router = APIRouter()

# ─── QUERY OPTIMIZATION: Bulk Operations ───────────────────────────────────────

@router.post('/bulk/assign-issues')
async def bulk_assign_issues(
    issue_ids: list[str],
    worker_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Bulk assign multiple issues to a worker
    Optimized: Single query with batch update
    """
    try:
        count = db.query(Issue).filter(Issue.id.in_(issue_ids)).update(
            {'assigned_to_id': worker_id, 'updated_at': datetime.utcnow()},
            synchronize_session=False,
        )
        db.commit()

        return {
            'success': True,
            'count': count,
            'message': f'{count} issues assigned to worker',
        }
    except Exception as e:
        db.rollback()
        raise Exception(f'Bulk assign failed: {str(e)}')


@router.post('/bulk/mark-resolved')
async def bulk_mark_resolved(
    issue_ids: list[str],
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Mark multiple issues as resolved in batch
    Optimized: Single UPDATE query
    """
    try:
        count = db.query(Issue).filter(Issue.id.in_(issue_ids)).update(
            {
                'status': 'resolved',
                'resolved_at': datetime.utcnow(),
                'updated_at': datetime.utcnow(),
            },
            synchronize_session=False,
        )
        db.commit()

        return {
            'success': True,
            'count': count,
            'message': f'{count} issues marked as resolved',
        }
    except Exception as e:
        db.rollback()
        raise Exception(f'Bulk mark resolved failed: {str(e)}')


# ─── ANALYTICS WITH OPTIMIZED QUERIES ──────────────────────────────────────────

@router.get('/analytics/dashboard')
async def get_dashboard_analytics(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Get dashboard analytics with optimized COUNT and GROUP BY queries
    """
    try:
        start_date = datetime.utcnow() - timedelta(days=days)

        # Optimized: Single query with COUNT
        total_issues = db.query(func.count(Issue.id)).scalar() or 0
        resolved_issues = (
            db.query(func.count(Issue.id))
            .filter(Issue.status == 'resolved')
            .scalar()
            or 0
        )
        open_issues = (
            db.query(func.count(Issue.id))
            .filter(Issue.status == 'open')
            .scalar()
            or 0
        )
        in_progress = (
            db.query(func.count(Issue.id))
            .filter(Issue.status == 'in_progress')
            .scalar()
            or 0
        )

        # Worker stats
        active_workers = (
            db.query(func.count(User.id))
            .filter(User.role == "worker", User.is_active == True)
            .scalar()
            or 0
        )

        # Citizens stats
        total_citizens = (
            db.query(func.count(User.id))
            .filter(User.role == "citizen")
            .scalar()
            or 0
        )

        # Issue type distribution
        type_distribution = (
            db.query(
                Issue.issue_type,
                func.count(Issue.id).label('count'),
            )
            .filter(Issue.created_at >= start_date)
            .group_by(Issue.issue_type)
            .all()
        )

        # Average resolution time (days)
        avg_resolution_time = (
            db.query(
                func.avg(
                    (
                        func.extract('day', Issue.resolved_at - Issue.created_at)
                    )
                )
            )
            .filter(
                and_(
                    Issue.status == 'resolved',
                    Issue.resolved_at >= start_date,
                )
            )
            .scalar()
            or 0
        )

        return {
            'summary': {
                'total_issues': total_issues,
                'resolved_issues': resolved_issues,
                'open_issues': open_issues,
                'in_progress': in_progress,
                'active_workers': active_workers,
                'total_citizens': total_citizens,
            },
            'type_distribution': [
                {'type': t[0], 'count': t[1]} for t in type_distribution
            ],
            'avg_resolution_days': round(float(avg_resolution_time), 2),
            'period_days': days,
        }
    except Exception as e:
        raise Exception(f'Analytics failed: {str(e)}')


# ─── NEW ENDPOINTS: Advanced Features ───────────────────────────────────────────

@router.get('/search/advanced')
async def advanced_search(
    query: str = Query('', min_length=2, max_length=100),
    issue_type: str = None,
    status: str = None,
    priority: str = None,
    ward: str = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Advanced search with filters
    Optimized: Selective loading of relationships
    """
    try:
        q = db.query(Issue)

        if query:
            q = q.filter(
                or_(
                    Issue.title.ilike(f'%{query}%'),
                    Issue.description.ilike(f'%{query}%'),
                )
            )

        if issue_type:
            q = q.filter(Issue.issue_type == issue_type)
        if status:
            q = q.filter(Issue.status == status)
        if priority:
            q = q.filter(Issue.priority == priority)
        if ward:
            q = q.filter(Issue.ward == ward)

        results = q.options(
            selectinload(Issue.assigned_to),
            selectinload(Issue.created_by),
        ).limit(limit).all()

        return {
            'count': len(results),
            'results': [
                {
                    'id': i.id,
                    'title': i.title,
                    'type': i.issue_type,
                    'status': i.status,
                    'priority': i.priority,
                    'assigned_to': i.assigned_to.name if i.assigned_to else None,
                    'created_at': i.created_at.isoformat(),
                }
                for i in results
            ],
        }
    except Exception as e:
        raise Exception(f'Search failed: {str(e)}')


@router.get('/metrics/performance')
async def get_performance_metrics(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Get performance metrics for dashboard
    Resolution rate, average time, worker efficiency
    """
    try:
        total = db.query(func.count(Issue.id)).scalar() or 1
        resolved = (
            db.query(func.count(Issue.id))
            .filter(Issue.status == 'resolved')
            .scalar()
            or 0
        )
        resolution_rate = round((resolved / total) * 100, 2) if total > 0 else 0

        # Top performing workers
        top_workers = (
            db.query(
                User.id,
                User.name,
                func.count(Issue.id).label('resolved_count'),
            )
            .filter(User.role == "worker")
            .outerjoin(Issue, Issue.assigned_worker_id == User.id)
            .filter(Issue.status == 'resolved')
            .group_by(User.id, User.name)
            .order_by(func.count(Issue.id).desc())
            .limit(10)
            .all()
        )

        return {
            'resolution_rate': f'{resolution_rate}%',
            'total_issues': total,
            'resolved_issues': resolved,
            'open_issues': total - resolved,
            'top_workers': [
                {'id': w[0], 'name': w[1], 'resolved': w[2]} for w in top_workers
            ],
        }
    except Exception as e:
        raise Exception(f'Metrics failed: {str(e)}')


# ─── BACKGROUND TASKS: Async Operations ───────────────────────────────────────

@router.post('/tasks/cleanup-resolved')
async def cleanup_resolved_issues(
    days: int = Query(90, ge=1),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Archive/delete resolved issues older than X days
    Runs in background to avoid blocking
    """

    def cleanup_task():
        try:
            threshold = datetime.utcnow() - timedelta(days=days)
            count = (
                db.query(Issue)
                .filter(
                    and_(
                        Issue.status == 'resolved',
                        Issue.resolved_at < threshold,
                    )
                )
                .delete(synchronize_session=False)
            )
            db.commit()
            print(f'Cleaned up {count} old resolved issues')
        except Exception as e:
            db.rollback()
            print(f'Cleanup failed: {str(e)}')

    background_tasks.add_task(cleanup_task)

    return {
        'message': 'Cleanup scheduled',
        'days': days,
        'status': 'processing',
    }


# ─── CACHING HELPERS ───────────────────────────────────────────────────────────

def get_cache_key(endpoint: str, filters: dict) -> str:
    """Generate cache key for query results"""
    filter_str = '|'.join(f'{k}:{v}' for k, v in sorted(filters.items()))
    return hashlib.md5(f'{endpoint}:{filter_str}'.encode()).hexdigest()


# Optional: Redis caching decorator (can be implemented for production)
# @cache.cached(timeout=300, key_prefix='civic_')
# @router.get('/cached/issues')
# async def get_cached_issues(...):
#     ...


