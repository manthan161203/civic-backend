"""
Geofence activity: who is in a zone, and what has been broadcast to it.
=======================================================================

Two gaps, both of which existed because the data was never written down.

**Workers in a zone** was not derivable from any endpoint, even though every
piece was present: ``User`` carries ``latitude``/``longitude``, and
``point_in_geofence`` has been in ``geofence_utils`` all along.

The honest caveat, which the endpoint states in its payload rather than only in
prose: a worker's position is a **single mutable point**, overwritten on each
update, with no history table. So this reports where each worker last checked
in. A phone that died three hours ago inside the zone would otherwise read as
someone standing there, so every row carries ``location_age_minutes`` and a
``stale`` flag.

**Alerts per zone** did not exist at all. ``POST /admin/notifications/geofence``
pushed to everyone in a circle, logged a count, and persisted nothing — so a
broadcast that reached nobody was indistinguishable from one that reached
everybody, and no history could be reconstructed afterwards.
"""

import uuid
from datetime import timedelta

from app.core.time import now_utc
from app.models.geofence import Geofence
from app.models.geofence_alert import GeofenceAlert

from tests.conftest import auth_header, make_user

# The zone every test below uses: 2 km around a point in Ahmedabad.
ZONE_LAT, ZONE_LNG, ZONE_RADIUS = 23.0225, 72.5714, 2.0


def _zone(db, creator, hierarchy, **kwargs):
    defaults = dict(
        id=uuid.uuid4(),
        name="Depot",
        latitude=ZONE_LAT,
        longitude=ZONE_LNG,
        radius_km=ZONE_RADIUS,
        created_by_id=creator.id,
        ward_id=hierarchy["ward"].id,
        taluka_id=hierarchy["taluka"].id,
        district_id=hierarchy["taluka"].district_id,
    )
    defaults.update(kwargs)
    gf = Geofence(**defaults)
    db.add(gf)
    db.flush()
    return gf


def _worker_at(db, lat, lng, hierarchy, *, minutes_ago=1, **kwargs):
    return make_user(
        db,
        "worker",
        latitude=lat,
        longitude=lng,
        location_updated_at=now_utc() - timedelta(minutes=minutes_ago),
        ward_id=hierarchy["ward"].id,
        **kwargs,
    )


def _workers_in(client, user, zone):
    response = client.get(
        f"/admin/geofences/{zone.id}/workers", headers=auth_header(user)
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── who is in the zone ────────────────────────────────────────────────────────


def test_worker_inside_the_radius_is_listed(client, db, super_admin, hierarchy):
    zone = _zone(db, super_admin, hierarchy)
    inside = _worker_at(db, ZONE_LAT + 0.005, ZONE_LNG, hierarchy, name="Inside")

    body = _workers_in(client, super_admin, zone)
    assert [w["name"] for w in body["workers"]] == ["Inside"]
    assert body["total"] == 1
    assert str(inside.id) == body["workers"][0]["id"]


def test_worker_outside_the_radius_is_not_listed(client, db, super_admin, hierarchy):
    """Guards the bounding-box prefilter as well as the haversine check."""
    zone = _zone(db, super_admin, hierarchy)
    _worker_at(db, ZONE_LAT + 0.5, ZONE_LNG + 0.5, hierarchy, name="Far away")

    assert _workers_in(client, super_admin, zone)["total"] == 0


def test_workers_without_a_location_are_not_listed(client, db, super_admin, hierarchy):
    zone = _zone(db, super_admin, hierarchy)
    make_user(db, "worker", name="Never reported", ward_id=hierarchy["ward"].id)

    assert _workers_in(client, super_admin, zone)["total"] == 0


def test_citizens_in_the_zone_are_not_listed(client, db, super_admin, hierarchy):
    zone = _zone(db, super_admin, hierarchy)
    make_user(
        db, "citizen", name="Resident",
        latitude=ZONE_LAT, longitude=ZONE_LNG,
        location_updated_at=now_utc(),
    )

    assert _workers_in(client, super_admin, zone)["total"] == 0


def test_inactive_workers_are_not_listed(client, db, super_admin, hierarchy):
    zone = _zone(db, super_admin, hierarchy)
    _worker_at(db, ZONE_LAT, ZONE_LNG, hierarchy, name="Deactivated", is_active=False)

    assert _workers_in(client, super_admin, zone)["total"] == 0


def test_workers_are_sorted_by_distance(client, db, super_admin, hierarchy):
    zone = _zone(db, super_admin, hierarchy)
    _worker_at(db, ZONE_LAT + 0.010, ZONE_LNG, hierarchy, name="Further")
    _worker_at(db, ZONE_LAT + 0.001, ZONE_LNG, hierarchy, name="Nearer")

    names = [w["name"] for w in _workers_in(client, super_admin, zone)["workers"]]
    assert names == ["Nearer", "Further"]


# ── staleness is reported, not hidden ─────────────────────────────────────────


def test_a_stale_fix_is_flagged(client, db, super_admin, hierarchy):
    """The whole caveat of this endpoint.

    Location is one mutable point with no history, so "last seen here" is not
    "is here". A worker whose phone died in the zone hours ago must not read as
    present without qualification.
    """
    zone = _zone(db, super_admin, hierarchy)
    _worker_at(db, ZONE_LAT, ZONE_LNG, hierarchy, name="Phone died", minutes_ago=180)

    body = _workers_in(client, super_admin, zone)
    worker = body["workers"][0]
    assert worker["stale"] is True
    assert worker["location_age_minutes"] > 170
    assert body["fresh"] == 0
    assert body["total"] == 1, "still reported — flagged, not silently dropped"


def test_a_recent_fix_is_not_flagged(client, db, super_admin, hierarchy):
    zone = _zone(db, super_admin, hierarchy)
    _worker_at(db, ZONE_LAT, ZONE_LNG, hierarchy, name="Just checked in", minutes_ago=2)

    body = _workers_in(client, super_admin, zone)
    assert body["workers"][0]["stale"] is False
    assert body["fresh"] == 1


def test_a_missing_timestamp_counts_as_stale(client, db, super_admin, hierarchy):
    """An unknown age is not evidence of freshness."""
    zone = _zone(db, super_admin, hierarchy)
    make_user(
        db, "worker", name="No timestamp",
        latitude=ZONE_LAT, longitude=ZONE_LNG,
        location_updated_at=None, ward_id=hierarchy["ward"].id,
    )

    worker = _workers_in(client, super_admin, zone)["workers"][0]
    assert worker["location_age_minutes"] is None
    assert worker["stale"] is True


# ── scope ─────────────────────────────────────────────────────────────────────


def test_zone_outside_your_jurisdiction_is_404(
    client, db, super_admin, other_ward_admin, hierarchy
):
    zone = _zone(db, super_admin, hierarchy)

    response = client.get(
        f"/admin/geofences/{zone.id}/workers", headers=auth_header(other_ward_admin)
    )
    assert response.status_code == 404


def test_workers_outside_your_jurisdiction_are_not_returned(
    client, db, super_admin, ward_admin, hierarchy
):
    """Otherwise this endpoint locates another district's staff."""
    zone = _zone(db, super_admin, hierarchy)
    _worker_at(db, ZONE_LAT, ZONE_LNG, hierarchy, name="Mine")
    make_user(
        db, "worker", name="Another ward",
        latitude=ZONE_LAT, longitude=ZONE_LNG,
        location_updated_at=now_utc(), ward_id=hierarchy["other_ward"].id,
    )

    names = [w["name"] for w in _workers_in(client, ward_admin, zone)["workers"]]
    assert names == ["Mine"]


# ── alert history ─────────────────────────────────────────────────────────────


def _alerts(client, user, zone):
    response = client.get(
        f"/admin/geofences/{zone.id}/alerts", headers=auth_header(user)
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_zone_with_no_recorded_alerts_returns_empty(client, db, super_admin, hierarchy):
    zone = _zone(db, super_admin, hierarchy)

    body = _alerts(client, super_admin, zone)
    assert body["items"] == []
    assert body["total"] == 0
    assert "not recorded" in body["history_since"], (
        "empty must not be presented as 'never alerted' — pre-migration "
        "broadcasts left no trace"
    )


def test_broadcasting_to_a_zone_records_a_linked_alert(
    client, db, super_admin, hierarchy
):
    """The link is what makes per-zone history possible at all."""
    zone = _zone(db, super_admin, hierarchy)

    response = client.post(
        "/admin/notifications/geofence",
        params={"geofence_id": str(zone.id), "title": "Flood warning", "body": "Avoid the area"},
        headers=auth_header(super_admin),
    )
    assert response.status_code == 200, response.text
    assert response.json()["alert_id"]

    body = _alerts(client, super_admin, zone)
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Flood warning"
    assert body["items"][0]["sent_by_name"] == super_admin.name


def test_an_adhoc_broadcast_is_recorded_but_not_attributed_to_a_zone(
    client, db, super_admin, hierarchy
):
    """Matching a raw circle to a nearby saved zone would be a guess."""
    zone = _zone(db, super_admin, hierarchy)

    response = client.post(
        "/admin/notifications/geofence",
        params={
            "latitude": ZONE_LAT, "longitude": ZONE_LNG, "radius_km": ZONE_RADIUS,
            "title": "One-off", "body": "Ad-hoc circle",
        },
        headers=auth_header(super_admin),
    )
    assert response.status_code == 200, response.text

    # Recorded...
    row = db.query(GeofenceAlert).filter(GeofenceAlert.title == "One-off").one()
    assert row.geofence_id is None
    assert row.radius_km == ZONE_RADIUS
    # ...but not counted against the zone.
    assert _alerts(client, super_admin, zone)["total"] == 0


def test_delivery_counts_are_persisted(client, db, super_admin, hierarchy):
    """A broadcast that reached nobody used to look like one that reached all."""
    zone = _zone(db, super_admin, hierarchy)
    make_user(
        db, "citizen", name="Reachable",
        latitude=ZONE_LAT, longitude=ZONE_LNG,
        location_updated_at=now_utc(), fcm_token="token-abc",
    )

    client.post(
        "/admin/notifications/geofence",
        params={"geofence_id": str(zone.id), "title": "Counted", "body": "x"},
        headers=auth_header(super_admin),
    )

    row = db.query(GeofenceAlert).filter(GeofenceAlert.title == "Counted").one()
    assert row.recipients_notified == 1
    assert row.recipients_failed == 0


def test_deleting_a_zone_keeps_its_alert_history(client, db, super_admin, hierarchy):
    """SET NULL, not CASCADE — the record that people were alerted survives."""
    zone = _zone(db, super_admin, hierarchy)
    client.post(
        "/admin/notifications/geofence",
        params={"geofence_id": str(zone.id), "title": "Historic", "body": "x"},
        headers=auth_header(super_admin),
    )

    assert client.delete(
        f"/admin/geofences/{zone.id}", headers=auth_header(super_admin)
    ).status_code == 204

    row = db.query(GeofenceAlert).filter(GeofenceAlert.title == "Historic").one()
    assert row.geofence_id is None
    assert row.title == "Historic"


# ── broadcast targeting validation ────────────────────────────────────────────


def test_supplying_both_a_zone_and_a_circle_is_rejected(
    client, db, super_admin, hierarchy
):
    zone = _zone(db, super_admin, hierarchy)

    response = client.post(
        "/admin/notifications/geofence",
        params={
            "geofence_id": str(zone.id), "latitude": 23.0, "longitude": 72.0,
            "radius_km": 1.0, "title": "Ambiguous", "body": "x",
        },
        headers=auth_header(super_admin),
    )
    assert response.status_code == 400, response.text


def test_supplying_neither_is_rejected(client, super_admin):
    response = client.post(
        "/admin/notifications/geofence",
        params={"title": "Aimless", "body": "x"},
        headers=auth_header(super_admin),
    )
    assert response.status_code == 400, response.text


def test_broadcasting_to_a_zone_outside_your_jurisdiction_is_404(
    client, db, super_admin, other_ward_admin, hierarchy
):
    zone = _zone(db, super_admin, hierarchy)

    response = client.post(
        "/admin/notifications/geofence",
        params={"geofence_id": str(zone.id), "title": "Not yours", "body": "x"},
        headers=auth_header(other_ward_admin),
    )
    assert response.status_code == 404, response.text
