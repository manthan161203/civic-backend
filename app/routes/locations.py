"""
Location Routes — District / Taluka / Ward hierarchy
=====================================================
Public read endpoints for populating registration dropdowns.
Admin-only write endpoints for creating/updating the location tree.

Hierarchy:
    State (configured in District.state_name)
    └── District
        └── Taluka
            └── Ward
"""

import math
import uuid
from typing import List, Optional

import requests as http_requests
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_role, require_any_admin
from app.core.logger import get_logger
from app.database import get_db
from app.models.location import District, Taluka, Ward
from app.models.user import User

logger = get_logger("locations")

router = APIRouter(prefix="/locations", tags=["Locations"])


# ── Geocoding helper ─────────────────────────────────────────────────────────

def _geocode(query: str) -> tuple[float, float] | None:
    """Look up centroid coordinates using OpenStreetMap Nominatim.

    Returns (lat, lon) on success, None if not found or request fails.
    """
    try:
        resp = http_requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "CivicApp/1.0"},
            timeout=5,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None


def _geocode_district(district_name: str, state_name: str = "Gujarat") -> tuple[float, float] | None:
    return _geocode(f"{district_name}, {state_name}, India")


def _geocode_taluka(taluka_name: str, district_name: str, state_name: str = "Gujarat") -> tuple[float, float] | None:
    return _geocode(f"{taluka_name}, {district_name}, {state_name}, India")


def _geocode_ward(ward_name: str, taluka_name: str, district_name: str) -> tuple[float, float] | None:
    return _geocode(f"{ward_name}, {taluka_name}, {district_name}, India")


# ── Response helpers ──────────────────────────────────────────────────────────

def _district_out(d: District) -> dict:
    return {
        "id": str(d.id),
        "name": d.name,
        "state_name": d.state_name,
        "centroid_lat": d.centroid_lat,
        "centroid_lon": d.centroid_lon,
        "geocoded": d.centroid_lat is not None and d.centroid_lon is not None,
    }


def _taluka_out(t: Taluka) -> dict:
    return {
        "id": str(t.id),
        "name": t.name,
        "district_id": str(t.district_id),
        "centroid_lat": t.centroid_lat,
        "centroid_lon": t.centroid_lon,
        "geocoded": t.centroid_lat is not None and t.centroid_lon is not None,
    }


def _ward_out(w: Ward) -> dict:
    return {
        "id": str(w.id),
        "name": w.name,
        "ward_number": w.ward_number,
        "taluka_id": str(w.taluka_id),
        "centroid_lat": w.centroid_lat,
        "centroid_lon": w.centroid_lon,
        "geocoded": w.centroid_lat is not None and w.centroid_lon is not None,
    }


# ── Name Suggestion / Autocomplete ──────────────────────────────────────────

@router.get("/suggest")
def suggest_locations(
    q: str = Query(..., min_length=1, max_length=100, description="Search prefix or substring"),
    type: str = Query("all", description="district | taluka | ward | all"),
    district_id: Optional[uuid.UUID] = Query(None, description="Scope taluka/ward suggestions to a district"),
    taluka_id: Optional[uuid.UUID] = Query(None, description="Scope ward suggestions to a taluka"),
    limit: int = Query(8, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Return name suggestions from the DB for autocomplete fields.

    Searches existing District, Taluka and/or Ward names matching the query
    prefix (case-insensitive). Use ``type`` to restrict results to a specific
    level. Use ``district_id``/``taluka_id`` to scope child suggestions.

    **Public endpoint** — safe for use in registration and report flows.

    Returns::

        [
            {"type": "district", "id": "...", "name": "Rajkot", "path": "Rajkot"},
            {"type": "taluka",   "id": "...", "name": "Rajkot", "path": "Rajkot / Rajkot"},
            {"type": "ward",     "id": "...", "name": "Ward 1", "path": "Rajkot / Rajkot / Ward 1"},
        ]
    """
    like = f"%{q}%"
    suggestions = []

    if type in ("district", "all"):
        rows = (
            db.query(District)
            .filter(District.name.ilike(like))
            .order_by(District.name)
            .limit(limit)
            .all()
        )
        for d in rows:
            suggestions.append({
                "type": "district",
                "id": str(d.id),
                "name": d.name,
                "path": d.name,
                "centroid_lat": d.centroid_lat,
                "centroid_lon": d.centroid_lon,
            })

    if type in ("taluka", "all"):
        q_t = db.query(Taluka, District).join(District, Taluka.district_id == District.id)
        if district_id:
            q_t = q_t.filter(Taluka.district_id == district_id)
        q_t = q_t.filter(Taluka.name.ilike(like)).order_by(Taluka.name).limit(limit)
        for t, d in q_t.all():
            suggestions.append({
                "type": "taluka",
                "id": str(t.id),
                "name": t.name,
                "path": f"{d.name} / {t.name}",
                "district_id": str(t.district_id),
                "centroid_lat": t.centroid_lat,
                "centroid_lon": t.centroid_lon,
            })

    if type in ("ward", "all"):
        q_w = db.query(Ward, Taluka, District).join(Taluka, Ward.taluka_id == Taluka.id).join(District, Taluka.district_id == District.id)
        if taluka_id:
            q_w = q_w.filter(Ward.taluka_id == taluka_id)
        elif district_id:
            q_w = q_w.filter(Taluka.district_id == district_id)
        q_w = q_w.filter(Ward.name.ilike(like)).order_by(Ward.name).limit(limit)
        for w, t, d in q_w.all():
            suggestions.append({
                "type": "ward",
                "id": str(w.id),
                "name": w.name,
                "ward_number": w.ward_number,
                "path": f"{d.name} / {t.name} / {w.name}",
                "taluka_id": str(w.taluka_id),
                "centroid_lat": w.centroid_lat,
                "centroid_lon": w.centroid_lon,
            })

    # Sort: exact prefix matches first, then alphabetically
    q_lower = q.lower()
    suggestions.sort(key=lambda s: (0 if s["name"].lower().startswith(q_lower) else 1, s["name"]))
    return suggestions[:limit]


# ── Districts ─────────────────────────────────────────────────────────────────

@router.get("/districts")
def list_districts(db: Session = Depends(get_db)):
    """List all districts.

    **Public endpoint** — used by registration and issue-creation screens.
    """
    return [_district_out(d) for d in db.query(District).order_by(District.name).all()]


@router.post("/districts", status_code=status.HTTP_201_CREATED)
def create_district(
    name: str = Query(..., min_length=1, max_length=100),
    state_name: str = Query("Gujarat", min_length=1, max_length=100),
    centroid_lat: Optional[float] = Query(None, ge=-90, le=90),
    centroid_lon: Optional[float] = Query(None, ge=-180, le=180),
    _admin: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Create a new district. **Super-admin only.**"""
    existing = db.query(District).filter(District.name == name, District.state_name == state_name).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="District already exists")
    if centroid_lat is None or centroid_lon is None:
        coords = _geocode_district(name, state_name)
        if coords:
            centroid_lat, centroid_lon = coords
            logger.info(f"Auto-geocoded district '{name}': {centroid_lat}, {centroid_lon}")
        else:
            logger.warning(f"Geocoding failed for district '{name}'")
    d = District(id=uuid.uuid4(), name=name, state_name=state_name,
                 centroid_lat=centroid_lat, centroid_lon=centroid_lon)
    db.add(d)
    db.commit()
    db.refresh(d)
    logger.info(f"District created: {d.name} (id={d.id})")
    return _district_out(d)


@router.delete("/districts/{district_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_district(
    district_id: uuid.UUID,
    _admin: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Delete a district (cascades to talukas → wards). **Super-admin only.**"""
    d = db.query(District).filter(District.id == district_id).first()
    if not d:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="District not found")
    db.delete(d)
    db.commit()


@router.patch("/districts/{district_id}")
def update_district(
    district_id: uuid.UUID,
    name: str = Query(..., min_length=1, max_length=100),
    state_name: str = Query(None, min_length=1, max_length=100),
    centroid_lat: Optional[float] = Query(None, ge=-90, le=90),
    centroid_lon: Optional[float] = Query(None, ge=-180, le=180),
    _admin: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Update a district. **Super-admin only.**"""
    d = db.query(District).filter(District.id == district_id).first()
    if not d:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="District not found")

    if name != d.name:
        existing = db.query(District).filter(
            District.name == name,
            District.state_name == (state_name or d.state_name)
        ).first()
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="District already exists")

    d.name = name
    if state_name:
        d.state_name = state_name
    if centroid_lat is not None:
        d.centroid_lat = centroid_lat
    if centroid_lon is not None:
        d.centroid_lon = centroid_lon
    if d.centroid_lat is None or d.centroid_lon is None:
        coords = _geocode_district(d.name, d.state_name)
        if coords:
            d.centroid_lat, d.centroid_lon = coords
            logger.info(f"Auto-geocoded district '{d.name}': {d.centroid_lat}, {d.centroid_lon}")
    db.commit()
    db.refresh(d)
    logger.info(f"District updated: {d.name} (id={d.id})")
    return _district_out(d)


# ── Talukas ───────────────────────────────────────────────────────────────────

@router.get("/districts/{district_id}/talukas")
def list_talukas(district_id: uuid.UUID, db: Session = Depends(get_db)):
    """List all talukas in a district. **Public.**"""
    district = db.query(District).filter(District.id == district_id).first()
    if not district:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="District not found")
    return [_taluka_out(t) for t in db.query(Taluka).filter(Taluka.district_id == district_id).order_by(Taluka.name).all()]


@router.post("/districts/{district_id}/talukas", status_code=status.HTTP_201_CREATED)
def create_taluka(
    district_id: uuid.UUID,
    name: str = Query(..., min_length=1, max_length=100),
    centroid_lat: Optional[float] = Query(None, ge=-90, le=90),
    centroid_lon: Optional[float] = Query(None, ge=-180, le=180),
    current_admin: User = Depends(require_role("admin", "district_admin")),
    db: Session = Depends(get_db),
):
    """Create a taluka inside a district. **district_admin or super-admin.**"""
    # district_admin can only add talukas to their own district
    if current_admin.role == "district_admin" and current_admin.district_id != district_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage talukas within your assigned district.",
        )
    district = db.query(District).filter(District.id == district_id).first()
    if not district:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="District not found")
    existing = db.query(Taluka).filter(Taluka.district_id == district_id, Taluka.name == name).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Taluka already exists in this district")
    if centroid_lat is None or centroid_lon is None:
        coords = _geocode_taluka(name, district.name, district.state_name)
        if coords:
            centroid_lat, centroid_lon = coords
            logger.info(f"Auto-geocoded taluka '{name}': {centroid_lat}, {centroid_lon}")
        else:
            logger.warning(f"Geocoding failed for taluka '{name}'")
    t = Taluka(id=uuid.uuid4(), name=name, district_id=district_id,
               centroid_lat=centroid_lat, centroid_lon=centroid_lon)
    db.add(t)
    db.commit()
    db.refresh(t)
    logger.info(f"Taluka created: {t.name} in district {district_id}")
    return _taluka_out(t)


@router.delete("/talukas/{taluka_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_taluka(
    taluka_id: uuid.UUID,
    current_admin: User = Depends(require_role("admin", "district_admin")),
    db: Session = Depends(get_db),
):
    """Delete a taluka (cascades to wards). **district_admin or super-admin.**"""
    t = db.query(Taluka).filter(Taluka.id == taluka_id).first()
    if not t:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Taluka not found")
    if current_admin.role == "district_admin" and current_admin.district_id != t.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only manage talukas within your assigned district.")
    db.delete(t)
    db.commit()


@router.patch("/talukas/{taluka_id}")
def update_taluka(
    taluka_id: uuid.UUID,
    name: str = Query(..., min_length=1, max_length=100),
    centroid_lat: Optional[float] = Query(None, ge=-90, le=90),
    centroid_lon: Optional[float] = Query(None, ge=-180, le=180),
    current_admin: User = Depends(require_role("admin", "district_admin")),
    db: Session = Depends(get_db),
):
    """Update a taluka. **district_admin or super-admin.**"""
    t = db.query(Taluka).filter(Taluka.id == taluka_id).first()
    if not t:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Taluka not found")
    if current_admin.role == "district_admin" and current_admin.district_id != t.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only manage talukas within your assigned district.")

    if name != t.name:
        existing = db.query(Taluka).filter(
            Taluka.district_id == t.district_id,
            Taluka.name == name
        ).first()
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Taluka already exists in this district")

    t.name = name
    if centroid_lat is not None:
        t.centroid_lat = centroid_lat
    if centroid_lon is not None:
        t.centroid_lon = centroid_lon
    if t.centroid_lat is None or t.centroid_lon is None:
        district = db.query(District).filter(District.id == t.district_id).first()
        coords = _geocode_taluka(t.name, district.name if district else "", district.state_name if district else "Gujarat")
        if coords:
            t.centroid_lat, t.centroid_lon = coords
            logger.info(f"Auto-geocoded taluka '{t.name}': {t.centroid_lat}, {t.centroid_lon}")
    db.commit()
    db.refresh(t)
    logger.info(f"Taluka updated: {t.name} (id={t.id})")
    return _taluka_out(t)


# ── Wards ─────────────────────────────────────────────────────────────────────

@router.get("/talukas/{taluka_id}/wards")
def list_wards(taluka_id: uuid.UUID, db: Session = Depends(get_db)):
    """List all wards in a taluka. **Public.**"""
    taluka = db.query(Taluka).filter(Taluka.id == taluka_id).first()
    if not taluka:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Taluka not found")
    return [
        _ward_out(w)
        for w in db.query(Ward).filter(Ward.taluka_id == taluka_id).order_by(Ward.ward_number).all()
    ]


@router.post("/talukas/{taluka_id}/wards", status_code=status.HTTP_201_CREATED)
def create_ward(
    taluka_id: uuid.UUID,
    name: str = Query(..., min_length=1, max_length=100),
    ward_number: int = Query(..., ge=1),
    centroid_lat: Optional[float] = Query(None, ge=-90, le=90),
    centroid_lon: Optional[float] = Query(None, ge=-180, le=180),
    current_admin: User = Depends(require_role("admin", "district_admin", "taluka_admin")),
    db: Session = Depends(get_db),
):
    """Create a ward inside a taluka. **taluka_admin or above.**"""
    taluka = db.query(Taluka).filter(Taluka.id == taluka_id).first()
    if not taluka:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Taluka not found")
    # taluka_admin can only add wards to their own taluka
    if current_admin.role == "taluka_admin" and current_admin.taluka_id != taluka_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage wards within your assigned taluka.",
        )
    # district_admin can only add wards within their district
    if current_admin.role == "district_admin" and current_admin.district_id != taluka.district_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage wards within your assigned district.",
        )
    existing = db.query(Ward).filter(Ward.taluka_id == taluka_id, Ward.ward_number == ward_number).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ward number already exists in this taluka")
    if centroid_lat is None or centroid_lon is None:
        district = db.query(District).filter(District.id == taluka.district_id).first()
        coords = _geocode_ward(name, taluka.name, district.name if district else "")
        if coords:
            centroid_lat, centroid_lon = coords
            logger.info(f"Auto-geocoded ward '{name}': {centroid_lat}, {centroid_lon}")
        else:
            logger.warning(f"Geocoding failed for ward '{name}' — coordinates not set. Use PATCH or /geocode to set them manually.")
    w = Ward(id=uuid.uuid4(), name=name, ward_number=ward_number, taluka_id=taluka_id,
             centroid_lat=centroid_lat, centroid_lon=centroid_lon)
    db.add(w)
    db.commit()
    db.refresh(w)
    logger.info(f"Ward created: {w.name} (#{w.ward_number}) in taluka {taluka_id}")
    return _ward_out(w)


@router.delete("/wards/{ward_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ward(
    ward_id: uuid.UUID,
    current_admin: User = Depends(require_role("admin", "district_admin", "taluka_admin")),
    db: Session = Depends(get_db),
):
    """Delete a ward. **taluka_admin or above.**"""
    w = db.query(Ward).filter(Ward.id == ward_id).first()
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ward not found")
    taluka = db.query(Taluka).filter(Taluka.id == w.taluka_id).first()
    if current_admin.role == "taluka_admin" and current_admin.taluka_id != w.taluka_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only manage wards within your assigned taluka.")
    if current_admin.role == "district_admin" and taluka and current_admin.district_id != taluka.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only manage wards within your assigned district.")
    db.delete(w)
    db.commit()


@router.patch("/wards/{ward_id}")
def update_ward(
    ward_id: uuid.UUID,
    name: str = Query(..., min_length=1, max_length=100),
    ward_number: int = Query(None, ge=1),
    centroid_lat: Optional[float] = Query(None, ge=-90, le=90),
    centroid_lon: Optional[float] = Query(None, ge=-180, le=180),
    current_admin: User = Depends(require_role("admin", "district_admin", "taluka_admin")),
    db: Session = Depends(get_db),
):
    """Update a ward. **taluka_admin or above.**"""
    w = db.query(Ward).filter(Ward.id == ward_id).first()
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ward not found")
    taluka = db.query(Taluka).filter(Taluka.id == w.taluka_id).first()
    if current_admin.role == "taluka_admin" and current_admin.taluka_id != w.taluka_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only manage wards within your assigned taluka.")
    if current_admin.role == "district_admin" and taluka and current_admin.district_id != taluka.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only manage wards within your assigned district.")

    # Check for duplicate ward number in same taluka if changing it
    if ward_number and ward_number != w.ward_number:
        existing = db.query(Ward).filter(
            Ward.taluka_id == w.taluka_id,
            Ward.ward_number == ward_number
        ).first()
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ward number already exists in this taluka")

    w.name = name
    if ward_number:
        w.ward_number = ward_number
    if centroid_lat is not None:
        w.centroid_lat = centroid_lat
    if centroid_lon is not None:
        w.centroid_lon = centroid_lon
    # Auto-geocode if coords still missing after explicit params
    if w.centroid_lat is None or w.centroid_lon is None:
        taluka = db.query(Taluka).filter(Taluka.id == w.taluka_id).first()
        district = db.query(District).filter(District.id == taluka.district_id).first() if taluka else None
        coords = _geocode_ward(w.name, taluka.name if taluka else "", district.name if district else "")
        if coords:
            w.centroid_lat, w.centroid_lon = coords
            logger.info(f"Auto-geocoded ward '{w.name}': {w.centroid_lat}, {w.centroid_lon}")
    db.commit()
    db.refresh(w)
    logger.info(f"Ward updated: {w.name} (id={w.id})")
    return _ward_out(w)


# ── Geocode existing ward ─────────────────────────────────────────────────────

@router.post("/wards/{ward_id}/geocode")
def geocode_ward(
    ward_id: uuid.UUID,
    current_admin: User = Depends(require_role("admin", "district_admin", "taluka_admin")),
    db: Session = Depends(get_db),
):
    """Look up and save centroid coordinates for an existing ward by name using OSM Nominatim.
    **taluka_admin or above.**
    """
    w = db.query(Ward).filter(Ward.id == ward_id).first()
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ward not found")
    taluka = db.query(Taluka).filter(Taluka.id == w.taluka_id).first()
    district = db.query(District).filter(District.id == taluka.district_id).first() if taluka else None
    if current_admin.role == "taluka_admin" and current_admin.taluka_id != w.taluka_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only manage wards within your assigned taluka.")
    if current_admin.role == "district_admin" and district and current_admin.district_id != district.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only manage wards within your assigned district.")
    coords = _geocode_ward(w.name, taluka.name if taluka else "", district.name if district else "")
    if not coords:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Could not find coordinates for this ward name")
    w.centroid_lat, w.centroid_lon = coords
    db.commit()
    db.refresh(w)
    logger.info(f"Geocoded ward '{w.name}': {w.centroid_lat}, {w.centroid_lon}")
    return _ward_out(w)


# ── Full tree ─────────────────────────────────────────────────────────────────

@router.get("/tree")
def get_full_tree(db: Session = Depends(get_db)):
    """Return the full State → District → Taluka → Ward tree. **Public.**

    Useful for cascading dropdowns on the registration screen.
    
    **Optimization**: Single joined query instead of N+1 cascading queries.
    """
    from sqlalchemy.orm import joinedload
    
    # Single query fetching all districts with their talukas and wards via joins
    districts = (
        db.query(District)
        .options(
            joinedload(District.talukas).joinedload(Taluka.wards)
        )
        .order_by(District.name)
        .all()
    )
    
    result = []
    for d in districts:
        talukas_data = []
        # Talukas are already loaded via joinedload, no additional query
        for t in sorted(d.talukas, key=lambda x: x.name):
            wards_data = [
                _ward_out(w)
                for w in sorted(t.wards, key=lambda x: x.ward_number)
            ]
            talukas_data.append({**_taluka_out(t), "wards": wards_data})
        result.append({**_district_out(d), "talukas": talukas_data})
    return result


# ── Nearby Ward (for mobile) ───────────────────────────────────────────────────

@router.get("/nearby-ward")
def get_nearby_ward(
    latitude: float = Query(...),
    longitude: float = Query(...),
    radius_km: float = Query(10, ge=1, le=50, description="Search radius in km (default 10, max 50)"),
    db: Session = Depends(get_db),
):
    """Find the ward closest to given coordinates within a specified radius using Haversine distance. **Public.**

    Only considers wards that have centroid coordinates set and are within the specified radius.
    
    **Query params**:
    - `latitude`: Required, GPS latitude.
    - `longitude`: Required, GPS longitude.
    - `radius_km`: Optional, search radius in km (default 10, max 50).

    Returns: {"id": ward_id, "name": ward_name, "taluka_id": taluka_id, "distance_km": distance} 
             or null if no ward found within radius.
    """
    EARTH_RADIUS_KM = 6371
    
    wards = db.query(Ward).filter(
        Ward.centroid_lat.isnot(None),
        Ward.centroid_lon.isnot(None),
    ).all()

    if not wards:
        return None

    lat_r = math.radians(latitude)
    lon_r = math.radians(longitude)

    def haversine_distance_km(w: Ward) -> float:
        """Calculate distance in km using Haversine formula."""
        dlat = math.radians(w.centroid_lat) - lat_r
        dlon = math.radians(w.centroid_lon) - lon_r
        a = math.sin(dlat / 2) ** 2 + math.cos(lat_r) * math.cos(math.radians(w.centroid_lat)) * math.sin(dlon / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return EARTH_RADIUS_KM * c

    # Calculate distances for all wards and filter by radius
    wards_with_distance = []
    for w in wards:
        dist = haversine_distance_km(w)
        if dist <= radius_km:
            wards_with_distance.append((w, dist))

    if not wards_with_distance:
        return None

    # Return the closest ward within the radius
    nearest_ward, nearest_distance = min(wards_with_distance, key=lambda x: x[1])
    return {
        "id": str(nearest_ward.id),
        "name": nearest_ward.name,
        "taluka_id": str(nearest_ward.taluka_id),
        "distance_km": round(nearest_distance, 2),
    }
