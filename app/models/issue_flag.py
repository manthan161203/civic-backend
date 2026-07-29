"""
IssueFlag Model — Reporting inappropriate issues or comments
============================================================
Citizens can flag issues or comments for admin review.
Admins then mark flags as reviewed or dismissed.
"""

import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class IssueFlag(Base):
    __tablename__ = "issue_flags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reporter_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Either issue_id or comment_id is set (not both)
    issue_id = Column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    comment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("issue_comments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    reason = Column(
        Enum("spam", "inappropriate", "duplicate", "false_report", "other", name="flag_reason"),
        nullable=False,
    )
    details = Column(Text, nullable=True)
    status = Column(
        Enum("pending", "reviewed", "dismissed", name="flag_status"),
        nullable=False,
        default="pending",
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    reporter = relationship("User")
    issue = relationship("Issue")
    comment = relationship("IssueComment")
