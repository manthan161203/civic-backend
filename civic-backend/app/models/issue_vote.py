"""
IssueVote Model — Citizens upvoting/endorsing issues
======================================================
Each citizen can upvote an issue once. The upvote_count on Issue is
a denormalized counter updated on insert/delete of votes.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class IssueVote(Base):
    __tablename__ = "issue_votes"

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

    __table_args__ = (UniqueConstraint("issue_id", "user_id", name="uq_issue_vote"),)

    issue = relationship("Issue", back_populates="votes")
    user = relationship("User")
