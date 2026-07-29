"""
Geofences belong to a jurisdiction.
===================================

``list_geofences`` documented the gap accurately — "Current Geofence model does
not track ward_id/taluka_id/district_id" — and then made a claim that was simply
false: "access control is enforced at creation/update/delete time". It was not.
``create_geofence``, ``update_geofence`` and ``delete_geofence`` each took a
bare ``require_any_admin``, so a single-ward admin could edit, and permanently
delete, any zone in the state.

Three columns and ``apply_admin_scope`` close it. What the tests below pin,
beyond the obvious:

* **A scoped admin cannot choose a zone's jurisdiction.** Honouring a
  caller-supplied ``ward_id`` would let any admin create — and thereafter
  manage — a zone anywhere, which is the same self-widening shape that
  ``PUT /auth/profile`` had.
* **The chain is denormalised.** A ward-level zone stores its taluka and
  district too, because ``apply_admin_scope`` filters on the single column
  matching the caller's tier; storing only ``ward_id`` would hide the zone from
  that ward's own taluka_admin.
* **Out of scope reads as 404, not 403**, so the endpoint cannot be used to
  enumerate other districts' zones.
* **Legacy all-NULL rows are state-level**, visible only to a super-admin. There
  is no boundary geometry to derive a jurisdiction from — wards carry centroids
  only — so back-filling would have meant guessing, and a wrong guess hands out
  delete rights.
"""

import uuid

from app.models.geofence import Geofence

from tests.conftest import auth_header, make_user


def _make_geofence(db, creator, **kwargs):
    defaults = dict(
        id=uuid.uuid4(),
        name="Zone A",
        latitude=23.02,
        longitude=72.57,
        radius_km=1.0,
        created_by_id=creator.id,
    )
    defaults.update(kwargs)
    gf = Geofence(**defaults)
    db.add(gf)
    db.flush()
    return gf


def _in_ward(db, creator, hierarchy, key="ward", **kwargs):
    """A zone stamped with a full ward → taluka → district chain."""
    ward = hierarchy[key]
    taluka = hierarchy["taluka" if key == "ward" else "other_taluka"]
    return _make_geofence(
        db,
        creator,
        ward_id=ward.id,
        taluka_id=taluka.id,
        district_id=taluka.district_id,
        **kwargs,
    )


def _list(client, user):
    response = client.get("/admin/geofences", headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _ids(items):
    return {item["id"] for item in items}


# ── visibility ────────────────────────────────────────────────────────────────


def test_ward_admin_sees_only_their_own_ward(client, db, ward_admin, other_ward_admin, hierarchy):
    mine = _in_ward(db, ward_admin, hierarchy, "ward", name="Mine")
    theirs = _in_ward(db, other_ward_admin, hierarchy, "other_ward", name="Theirs")

    visible = _ids(_list(client, ward_admin))
    assert str(mine.id) in visible
    assert str(theirs.id) not in visible


def test_taluka_admin_sees_a_ward_level_zone_beneath_them(
    client, db, ward_admin, taluka_admin, hierarchy
):
    """This is why the chain is denormalised rather than storing only ward_id."""
    zone = _in_ward(db, ward_admin, hierarchy, "ward")

    assert str(zone.id) in _ids(_list(client, taluka_admin))


def test_super_admin_sees_everything(client, db, ward_admin, super_admin, hierarchy):
    scoped = _in_ward(db, ward_admin, hierarchy, "ward")
    state_level = _make_geofence(db, super_admin, name="Statewide")

    visible = _ids(_list(client, super_admin))
    assert {str(scoped.id), str(state_level.id)} <= visible


def test_legacy_unscoped_zones_are_super_admin_only(client, db, super_admin, ward_admin):
    """All-NULL is state-level. Every pre-migration row is in this category."""
    legacy = _make_geofence(db, super_admin, name="Pre-migration zone")

    assert str(legacy.id) in _ids(_list(client, super_admin))
    assert str(legacy.id) not in _ids(_list(client, ward_admin))


def test_pagination_total_respects_scope(client, db, ward_admin, other_ward_admin, hierarchy):
    """`total` must count the scoped query, not the whole table."""
    _in_ward(db, ward_admin, hierarchy, "ward")
    _in_ward(db, other_ward_admin, hierarchy, "other_ward")
    _in_ward(db, other_ward_admin, hierarchy, "other_ward")

    response = client.get("/admin/geofences", headers=auth_header(ward_admin))
    assert response.json()["total"] == 1


# ── mutation is confined to the caller's jurisdiction ─────────────────────────


def test_ward_admin_cannot_delete_another_wards_zone(
    client, db, ward_admin, other_ward_admin, hierarchy
):
    """The headline hole: this used to succeed, and a delete is irreversible."""
    theirs = _in_ward(db, other_ward_admin, hierarchy, "other_ward")

    response = client.delete(
        f"/admin/geofences/{theirs.id}", headers=auth_header(ward_admin)
    )
    assert response.status_code == 404, response.text
    assert db.query(Geofence).filter(Geofence.id == theirs.id).first() is not None


def test_ward_admin_cannot_edit_another_wards_zone(
    client, db, ward_admin, other_ward_admin, hierarchy
):
    theirs = _in_ward(db, other_ward_admin, hierarchy, "other_ward", name="Theirs")

    response = client.patch(
        f"/admin/geofences/{theirs.id}",
        json={"name": "Hijacked"},
        headers=auth_header(ward_admin),
    )
    assert response.status_code == 404, response.text

    db.refresh(theirs)
    assert theirs.name == "Theirs"


def test_out_of_scope_is_indistinguishable_from_missing(
    client, db, ward_admin, other_ward_admin, hierarchy
):
    """403 would confirm the zone exists, making this an enumeration oracle."""
    theirs = _in_ward(db, other_ward_admin, hierarchy, "other_ward")

    real = client.delete(f"/admin/geofences/{theirs.id}", headers=auth_header(ward_admin))
    absent = client.delete(f"/admin/geofences/{uuid.uuid4()}", headers=auth_header(ward_admin))

    assert real.status_code == absent.status_code == 404
    assert real.json() == absent.json()


def test_ward_admin_can_manage_their_own_zone(client, db, ward_admin, hierarchy):
    mine = _in_ward(db, ward_admin, hierarchy, "ward")

    edit = client.patch(
        f"/admin/geofences/{mine.id}",
        json={"name": "Renamed"},
        headers=auth_header(ward_admin),
    )
    assert edit.status_code == 200, edit.text

    delete = client.delete(f"/admin/geofences/{mine.id}", headers=auth_header(ward_admin))
    assert delete.status_code == 204, delete.text


def test_ward_admin_cannot_touch_a_state_level_zone(client, db, ward_admin, super_admin):
    legacy = _make_geofence(db, super_admin)

    assert client.delete(
        f"/admin/geofences/{legacy.id}", headers=auth_header(ward_admin)
    ).status_code == 404


# ── creation stamps the caller's own scope ────────────────────────────────────


def test_created_zone_inherits_the_creators_scope(client, db, ward_admin, hierarchy):
    response = client.post(
        "/admin/geofences",
        json={"name": "New zone", "latitude": 21.1, "longitude": 70.1, "radius_km": 1.0},
        headers=auth_header(ward_admin),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["ward_id"] == str(hierarchy["ward"].id)
    assert body["taluka_id"] == str(hierarchy["taluka"].id)
    assert body["district_id"] is not None, "the chain must be denormalised"


def test_scoped_admin_cannot_choose_a_different_jurisdiction(
    client, db, ward_admin, hierarchy
):
    """Otherwise any admin could create, and then manage, a zone anywhere."""
    response = client.post(
        "/admin/geofences",
        json={
            "name": "Land grab",
            "latitude": 21.2,
            "longitude": 70.2,
            "radius_km": 1.0,
            "ward_id": str(hierarchy["other_ward"].id),
        },
        headers=auth_header(ward_admin),
    )
    assert response.status_code == 201, response.text
    assert response.json()["ward_id"] == str(hierarchy["ward"].id), (
        "the caller's own ward must win over the one they asked for"
    )


def test_super_admin_can_target_a_ward_and_gets_the_full_chain(
    client, db, super_admin, hierarchy
):
    response = client.post(
        "/admin/geofences",
        json={
            "name": "Targeted",
            "latitude": 21.3,
            "longitude": 70.3,
            "radius_km": 1.0,
            "ward_id": str(hierarchy["ward"].id),
        },
        headers=auth_header(super_admin),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["ward_id"] == str(hierarchy["ward"].id)
    assert body["taluka_id"] == str(hierarchy["taluka"].id)
    assert body["district_id"] is not None


def test_super_admin_creating_without_a_target_gets_a_state_level_zone(
    client, super_admin
):
    response = client.post(
        "/admin/geofences",
        json={"name": "Statewide", "latitude": 21.4, "longitude": 70.4, "radius_km": 1.0},
        headers=auth_header(super_admin),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["ward_id"], body["taluka_id"], body["district_id"]) == (None, None, None)


def test_super_admin_naming_a_missing_ward_is_rejected(client, super_admin):
    response = client.post(
        "/admin/geofences",
        json={
            "name": "Nowhere",
            "latitude": 21.5,
            "longitude": 70.5,
            "radius_km": 1.0,
            "ward_id": str(uuid.uuid4()),
        },
        headers=auth_header(super_admin),
    )
    assert response.status_code == 400, response.text


# ── the overlap check no longer leaks other jurisdictions ─────────────────────


def test_overlap_check_does_not_name_zones_the_caller_cannot_see(
    client, db, ward_admin, other_ward_admin, hierarchy
):
    """The 409 listed colliding zone names from the whole state.

    A ward_admin creating a zone learned the names of zones in districts they
    have no access to. Overlaps are now compared only within scope.
    """
    _in_ward(
        db, other_ward_admin, hierarchy, "other_ward",
        name="Secret Zone In Another District",
        latitude=25.0, longitude=75.0, radius_km=5.0,
    )

    response = client.post(
        "/admin/geofences",
        json={"name": "Mine", "latitude": 25.0, "longitude": 75.0, "radius_km": 5.0},
        headers=auth_header(ward_admin),
    )
    assert response.status_code == 201, response.text
    assert "Secret Zone" not in response.text


def test_overlap_is_still_caught_within_a_jurisdiction(client, db, ward_admin, hierarchy):
    """Scoping the check must not disable it."""
    _in_ward(
        db, ward_admin, hierarchy, "ward",
        name="Existing", latitude=25.0, longitude=75.0, radius_km=5.0,
    )

    response = client.post(
        "/admin/geofences",
        json={"name": "Overlapping", "latitude": 25.0, "longitude": 75.0, "radius_km": 5.0},
        headers=auth_header(ward_admin),
    )
    assert response.status_code == 409, response.text
    assert "Existing" in response.text


# ── a scoped admin with no scope assigned is denied, not widened ──────────────


def test_scoped_admin_without_a_scope_sees_nothing(client, db, super_admin):
    """`get_admin_scope_filter` returns DENY_ALL for this case; the geofence
    variant must not accidentally turn that into "sees everything".
    """
    stranded = make_user(db, "ward_admin", ward_id=None)
    _make_geofence(db, super_admin, name="Statewide")

    assert _list(client, stranded) == []
