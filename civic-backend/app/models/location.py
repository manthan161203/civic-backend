"""
Location Models — District / Taluka / Ward hierarchy
=====================================================
Represents the administrative geography:

    State (config)
    └── District
        └── Taluka
            └── Ward

Admins are scoped to one level of this hierarchy.
"""

import uuid

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class District(Base):
    __tablename__ = "districts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    state_name = Column(String(100), nullable=False, server_default="Gujarat")
    centroid_lat = Column(Float, nullable=True)
    centroid_lon = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    talukas = relationship("Taluka", back_populates="district", cascade="all, delete-orphan")


class Taluka(Base):
    __tablename__ = "talukas"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    district_id = Column(
        UUID(as_uuid=True),
        ForeignKey("districts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    centroid_lat = Column(Float, nullable=True)
    centroid_lon = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    district = relationship("District", back_populates="talukas")
    wards = relationship("Ward", back_populates="taluka", cascade="all, delete-orphan")


class Ward(Base):
    __tablename__ = "wards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    ward_number = Column(Integer, nullable=False)
    centroid_lat = Column(Float, nullable=True)
    centroid_lon = Column(Float, nullable=True)
    taluka_id = Column(
        UUID(as_uuid=True),
        ForeignKey("talukas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    taluka = relationship("Taluka", back_populates="wards")
