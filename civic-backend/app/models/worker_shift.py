"""
WorkerShift Model — Worker weekly shift schedule
=================================================
Defines the regular working hours for a worker per day of week.
Geo-routing respects shifts so workers are only assigned during active hours.

day_of_week: 0 = Monday, 1 = Tuesday, ... 6 = Sunday
start_time / end_time: "HH:MM" in 24-hour format (e.g. "09:00", "18:00")
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class WorkerShift(Base):
    __tablename__ = "worker_shifts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    day_of_week = Column(Integer, nullable=False)    # 0 = Monday … 6 = Sunday
    start_time = Column(String(5), nullable=False)   # "09:00"
    end_time = Column(String(5), nullable=False)     # "18:00"
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("worker_id", "day_of_week", name="uq_worker_shift_day"),
    )

    worker = relationship("User")
