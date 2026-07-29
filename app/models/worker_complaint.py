"""
WorkerComplaint Model — Citizens filing complaints against workers
==================================================================
Tracks citizen complaints about worker conduct or quality of work.
Admins review and resolve complaints from their dashboard.
"""

import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.database import Base


class WorkerComplaint(Base):
    __tablename__ = "worker_complaints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    citizen_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    issue_id = Column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reason = Column(
        Enum("rude_behavior", "poor_work", "delayed", "no_show", "other", name="complaint_reason"),
        nullable=False,
    )
    description = Column(Text, nullable=False)
    photos = Column(JSONB, default=list, nullable=False)
    status = Column(
        Enum("pending", "investigating", "resolved", "dismissed", name="complaint_status"),
        nullable=False,
        default="pending",
        index=True,
    )
    admin_notes = Column(Text, nullable=True)
    resolved_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    worker = relationship("User", foreign_keys=[worker_id])
    citizen = relationship("User", foreign_keys=[citizen_id])
    issue = relationship("Issue")
    resolver = relationship("User", foreign_keys=[resolved_by])
