"""
IssueBookmark Model — Citizens bookmarking issues they care about
=================================================================
Citizens can save/bookmark any issue to track its progress.
They receive notifications when bookmarked issues change status.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class IssueBookmark(Base):
    __tablename__ = "issue_bookmarks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("issue_id", "user_id", name="uq_issue_bookmark"),)

    issue = relationship("Issue")
    user = relationship("User")
