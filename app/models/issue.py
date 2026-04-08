"""
Issue Model — Civic issue reported by a citizen
================================================
Covers the full lifecycle of a civic complaint:

    open → assigned → in_progress → resolved → closed

Key design decisions:
- ``before_photos`` / ``after_photos`` are JSON arrays of URLs (Cloudinary or local).
- AI fields (``ai_*``) are populated asynchronously after photo upload — never set by clients.
- ``parent_issue_id`` links duplicates back to the original issue.
- ``ward_id`` FK is the structured reference; ``ward`` string is kept for display/backward compat.
- ``upvote_count`` is a denormalized counter updated by IssueVote inserts/deletes.
- Soft-delete: ``is_deleted=True`` hides issues from all views but preserves audit data.
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import relationship

from app.database import Base


class Issue(Base):
    __tablename__ = "issues"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reporter_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    assigned_worker_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    parent_issue_id = Column(UUID(as_uuid=True), ForeignKey("issues.id"), nullable=True)

    issue_type = Column(
        String(100),
        nullable=False,
        index=True,
    )
    severity = Column(
        Enum("high", "medium", "low", name="issue_severity"),
        nullable=False,
        default="medium",
    )
    priority = Column(
        Enum("urgent", "high", "medium", "low", name="issue_priority"),
        nullable=False,
        default="medium",
    )
    status = Column(
        Enum("open", "assigned", "in_progress", "resolved", "closed", name="issue_status"),
        nullable=False,
        default="open",
        index=True,
    )

    # Custom label when issue_type is "other" (citizen-provided)
    custom_issue_type_label = Column(String(100), nullable=True)

    # Department routing: matches worker department for targeted assignment
    department = Column(String(50), nullable=True, index=True)

    description = Column(Text, nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    address = Column(String, nullable=True)

    # Legacy string ward (display / backward compat)
    ward = Column(String, nullable=True, index=True)

    # Structured FK to Ward (used for hierarchy scoping)
    ward_id = Column(
        UUID(as_uuid=True),
        ForeignKey("wards.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    before_photos = Column(JSON, default=list, nullable=False)
    after_photos = Column(JSON, default=list, nullable=False)

    # AI classification results (before photo)
    ai_issue_type = Column(String, nullable=True)
    ai_severity = Column(String, nullable=True)
    ai_confidence = Column(Float, nullable=True)
    ai_suggested_description = Column(Text, nullable=True)

    # AI resolution verification results (after photo)
    ai_is_resolved = Column(Boolean, nullable=True)
    ai_resolution_quality = Column(String, nullable=True)   # good / partial / poor
    ai_resolution_notes = Column(Text, nullable=True)

    resolution_notes = Column(Text, nullable=True)
    citizen_rating = Column(Integer, nullable=True)

    # Upvotes (denormalized counter, incremented by trigger logic in app)
    upvote_count = Column(Integer, server_default="0", nullable=False)

    is_duplicate = Column(Boolean, default=False, nullable=False)
    # Escalation
    is_escalated = Column(Boolean, default=False, nullable=False)
    escalated_at = Column(DateTime(timezone=True), nullable=True)
    escalation_level = Column(Integer, server_default="0", nullable=False)  # 0=none, 1=ward, 2=taluka, 3=district
    # SOS / Emergency Hazard
    is_sos = Column(Boolean, default=False, nullable=False)
    sos_radius_notified = Column(Boolean, default=False, nullable=False)  # True once nearby citizens alerted

    # Blocked by worker — with admin resolution tracking
    is_blocked = Column(Boolean, default=False, nullable=False, index=True)
    blocked_reason = Column(Text, nullable=True)
    blocked_at = Column(DateTime(timezone=True), nullable=True)  # When worker blocked
    blocked_by_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)  # Worker who blocked
    
    # Admin unblock tracking
    unblocked_at = Column(DateTime(timezone=True), nullable=True)  # When admin unblocked
    unblocked_by_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)  # Admin who unblocked
    admin_unblock_note = Column(Text, nullable=True)  # Admin's reason for unblocking
    block_resolved_by = Column(String(50), nullable=True)  # 'reassign' | 'unblock' | 'resolve' | 'other'

    # Rejection tracking — auto-escalates after 3 rejections
    reassignment_count = Column(Integer, server_default="0", nullable=False)
    # JSON list of worker UUIDs (as strings) who have rejected this issue — used to
    # exclude all prior rejecters when finding the next worker, not just the last one.
    rejected_by_ids = Column(JSON, default=list, nullable=False, server_default="'[]'")

    # Soft-delete — admin can remove an issue without destroying citizen records
    is_deleted = Column(Boolean, server_default="false", nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    reporter = relationship("User", foreign_keys=[reporter_id], back_populates="reported_issues")
    assigned_worker = relationship("User", foreign_keys=[assigned_worker_id], back_populates="assigned_issues")
    notifications = relationship("Notification", back_populates="issue")
    comments = relationship(
        "IssueComment",
        back_populates="issue",
        cascade="all, delete-orphan",
        order_by="IssueComment.created_at",
    )
    votes = relationship("IssueVote", back_populates="issue", cascade="all, delete-orphan")

    @property
    def assigned_worker_name(self) -> str | None:
        if self.assigned_worker is not None:
            return self.assigned_worker.name or self.assigned_worker.phone
        return None
