"""
Dispute Model — Citizens disputing issue resolutions
=====================================================
When a citizen believes their issue was not properly resolved,
they can file a dispute with photo evidence. The dispute triggers
admin review and optionally reopens the issue.

Status flow: open → under_review → accepted → rejected
"""

import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.database import Base


class Dispute(Base):
    __tablename__ = "disputes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    citizen_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reason = Column(Text, nullable=False)
    photos = Column(JSONB, default=list, nullable=False)
    status = Column(
        Enum("open", "under_review", "accepted", "rejected", name="dispute_status"),
        nullable=False,
        default="open",
        index=True,
    )
    admin_notes = Column(Text, nullable=True)
    resolved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    issue = relationship("Issue")
    citizen = relationship("User", foreign_keys=[citizen_id])
    resolver = relationship("User", foreign_keys=[resolved_by])
