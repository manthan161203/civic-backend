"""
IssueSquad Model — Multi-worker team assignments for large issues
=================================================================
Allows creating squads for complex issues (e.g., fallen tree, major pipe burst)
where a lead worker coordinates with assistant workers.

Design:
- ``lead_worker_id`` is the primary responsible worker
- ``assistant_ids`` is a JSON array of additional worker UUIDs
- Links back to the parent ``Issue`` via ``issue_id``
- ``status`` tracks squad lifecycle: active → completed → disbanded
"""

import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import relationship

from app.database import Base


class IssueSquad(Base):
    __tablename__ = "issue_squads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    lead_worker_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # JSON array of assistant worker UUIDs (strings)
    assistant_ids = Column(JSON, default=list, nullable=False)
    status = Column(
        Enum("active", "completed", "disbanded", name="squad_status"),
        nullable=False,
        default="active",
    )
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    issue = relationship("Issue", backref="squad")
    lead_worker = relationship("User", foreign_keys=[lead_worker_id])
