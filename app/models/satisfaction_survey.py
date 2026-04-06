"""
SatisfactionSurvey Model — Post-resolution citizen feedback
============================================================
After rating an issue, citizens can optionally complete a short survey
providing more detailed feedback about the resolution experience.
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class SatisfactionSurvey(Base):
    __tablename__ = "satisfaction_surveys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    citizen_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fully_resolved = Column(Boolean, nullable=False)
    speed_rating = Column(
        Integer, nullable=False,
    )  # 1=too_slow, 2=acceptable, 3=fast
    would_report_again = Column(Boolean, nullable=False)
    feedback = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    issue = relationship("Issue")
    citizen = relationship("User")
