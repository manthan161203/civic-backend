"""
IssueComment Model — Interim updates on civic issues
=====================================================
Workers post progress updates ("arrived on site", "waiting for equipment").
Citizens and admins can follow the thread on their issues.
Each comment is scoped to a single issue and has a single author.
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import backref, relationship

from app.database import Base


class IssueComment(Base):
    __tablename__ = "issue_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("issue_comments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    body = Column(Text, nullable=False)
    is_internal = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    issue = relationship("Issue", back_populates="comments")
    author = relationship("User")
    replies = relationship(
        "IssueComment",
        backref=backref("parent", remote_side=[id]),
        foreign_keys=[parent_id],
        lazy="select",
    )