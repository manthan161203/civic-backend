"""
WardSubscription Model — Citizens subscribing to a ward's issues
================================================================
Citizens can subscribe to wards other than their own to receive
push notifications when new issues are filed in those wards.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class WardSubscription(Base):
    __tablename__ = "ward_subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ward_id = Column(
        UUID(as_uuid=True),
        ForeignKey("wards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "ward_id", name="uq_ward_subscription"),)

    user = relationship("User")
    ward = relationship("Ward")
