"""
Worker Complaint Routes
=======================
Citizens can file complaints against workers for poor conduct or quality.
Admins review complaints from their dashboard.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_any_admin, require_role
from app.core.logger import get_logger
from app.database import get_db
from app.models.issue import Issue
from app.models.user import User
from app.models.worker_complaint import WorkerComplaint
from app.services.notification_service import notify_localized
from app.services.storage import upload_image

logger = get_logger("complaints")

router = APIRouter(tags=["Worker Complaints"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class ComplaintCreate(BaseModel):
    worker_id: uuid.UUID = Field(..., description="UUID of the worker being complained about")
    issue_id: Optional[uuid.UUID] = Field(None, description="Related issue UUID (optional)")
    reason: str = Field(..., description="rude_behavior | poor_work | delayed | no_show | other")
    description: str = Field(..., min_length=10, max_length=2000)


class ComplaintResponse(BaseModel):
    id: uuid.UUID
    worker_id: uuid.UUID
    citizen_id: uuid.UUID
    issue_id: Optional[uuid.UUID] = None
    reason: str
    description: str
    photos: List[str]
    status: str
    admin_notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ComplaintResolve(BaseModel):
    status: str = Field(..., description="investigating | resolved | dismissed")
    admin_notes: Optional[str] = Field(None, max_length=2000)


# ── Citizen Endpoints ────────────────────────────────────────────────────────

@router.post("/workers/complaints", response_model=ComplaintResponse, status_code=201)
def file_complaint(
    body: ComplaintCreate,
    current_user: User = Depends(require_role("citizen", "admin")),
    db: Session = Depends(get_db),
):
    """File a complaint against a worker.

    **Roles**: citizen, admin.
    """
    allowed_reasons = ("rude_behavior", "poor_work", "delayed", "no_show", "other")
    if body.reason not in allowed_reasons:
        raise HTTPException(status_code=400, detail=f"reason must be one of: {', '.join(allowed_reasons)}")

    worker = db.query(User).filter(User.id == body.worker_id, User.role == "worker").first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    if body.issue_id:
        issue = db.query(Issue).filter(Issue.id == body.issue_id, Issue.is_deleted == False).first()
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

    try:
        complaint = WorkerComplaint(
            worker_id=body.worker_id,
            citizen_id=current_user.id,
            issue_id=body.issue_id,
            reason=body.reason,
            description=body.description,
        )
        db.add(complaint)
        db.commit()
        db.refresh(complaint)

        # Notify admins
        admins = db.query(User).filter(
            User.role.in_(["admin", "ward_admin", "taluka_admin", "district_admin"]),
            User.is_active == True,
        ).all()
        issue_id_short = str(body.issue_id)[:8] if body.issue_id else "N/A"
        for admin_user in admins:
            notify_localized(
                db=db, user=admin_user, key="worker_complaint",
                notification_type="system",
                issue_id=str(body.issue_id) if body.issue_id else None,
                action_type="open_complaint",
                issue_id_short=issue_id_short,
                ward=worker.ward or "unknown",
            )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error filing complaint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to file complaint.")

    logger.info(f"Complaint filed by {current_user.id} against worker {body.worker_id}")
    return ComplaintResponse.model_validate(complaint)


@router.post("/workers/complaints/{complaint_id}/photos", response_model=ComplaintResponse)
async def upload_complaint_photos(
    complaint_id: uuid.UUID,
    photos: List[UploadFile] = File(...),
    current_user: User = Depends(require_role("citizen", "admin")),
    db: Session = Depends(get_db),
):
    """Upload evidence photos for a complaint."""
    complaint = db.query(WorkerComplaint).filter(
        WorkerComplaint.id == complaint_id,
        WorkerComplaint.citizen_id == current_user.id,
    ).first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")

    try:
        uploaded_urls = []
        for photo in photos:
            file_bytes = await photo.read()
            if len(file_bytes) > 10 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="File too large. Max 10 MB.")
            filename = f"complaint_{uuid.uuid4().hex[:8]}"
            url = upload_image(file_bytes, filename, user_id=str(current_user.id))
            if url:
                uploaded_urls.append(url)

        complaint.photos = (complaint.photos or []) + uploaded_urls
        db.commit()
        db.refresh(complaint)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error uploading complaint photos: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Photo upload failed.")

    return ComplaintResponse.model_validate(complaint)


@router.get("/me/complaints", response_model=List[ComplaintResponse])
def get_my_complaints(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get complaints filed by the current user."""
    items = (
        db.query(WorkerComplaint)
        .filter(WorkerComplaint.citizen_id == current_user.id)
        .order_by(WorkerComplaint.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return [ComplaintResponse.model_validate(c) for c in items]


# ── Admin Endpoints ──────────────────────────────────────────────────────────

@router.get("/admin/complaints", response_model=List[ComplaintResponse])
def list_complaints(
    status_filter: Optional[str] = Query(None, alias="status"),
    worker_id: Optional[uuid.UUID] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List all worker complaints for admin review.

    **Roles**: any admin.
    """
    query = db.query(WorkerComplaint)
    if status_filter:
        query = query.filter(WorkerComplaint.status == status_filter)
    if worker_id:
        query = query.filter(WorkerComplaint.worker_id == worker_id)
    items = query.order_by(WorkerComplaint.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return [ComplaintResponse.model_validate(c) for c in items]


@router.post("/admin/complaints/{complaint_id}/resolve", response_model=ComplaintResponse)
def resolve_complaint(
    complaint_id: uuid.UUID,
    body: ComplaintResolve,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Resolve a worker complaint.

    **Roles**: any admin.
    """
    allowed = ("investigating", "resolved", "dismissed")
    if body.status not in allowed:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(allowed)}")

    complaint = db.query(WorkerComplaint).filter(WorkerComplaint.id == complaint_id).first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")

    try:
        complaint.status = body.status
        complaint.admin_notes = body.admin_notes
        complaint.resolved_by = current_user.id
        db.commit()
        db.refresh(complaint)
    except Exception as e:
        db.rollback()
        logger.error(f"Error resolving complaint {complaint_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resolve complaint.")

    logger.info(f"Admin {current_user.id} resolved complaint {complaint_id} as {body.status}")
    return ComplaintResponse.model_validate(complaint)


@router.get("/admin/workers/{worker_id}/complaints", response_model=dict)
def worker_complaint_summary(
    worker_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get complaint summary for a specific worker.

    **Roles**: any admin.
    """
    total = db.query(WorkerComplaint).filter(WorkerComplaint.worker_id == worker_id).count()
    pending = db.query(WorkerComplaint).filter(
        WorkerComplaint.worker_id == worker_id,
        WorkerComplaint.status == "pending",
    ).count()
    resolved = db.query(WorkerComplaint).filter(
        WorkerComplaint.worker_id == worker_id,
        WorkerComplaint.status == "resolved",
    ).count()

    return {
        "worker_id": str(worker_id),
        "total_complaints": total,
        "pending": pending,
        "resolved": resolved,
        "dismissed": total - pending - resolved,
    }
