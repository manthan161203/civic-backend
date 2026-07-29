"""`GET /admin/workers?pending_invite=` — the filter the console needed.

The admin console has a tab labelled "Invited workers". It was built on
``is_active=false``, which selects *deactivated* workers — the opposite group.
The two sets never intersect, because an invited worker is by definition still
active, so the tab listed people who could not be invited at all and hid
everyone who was genuinely waiting.

The tell was in the UI: the "Resend invite" button renders only when
``must_change_password and is_active``, so on a tab where every row had
``is_active == False`` the button could never appear. The one screen that
existed for chasing invitations could not chase one.

These tests pin the three populations apart.
"""

from tests.conftest import auth_header, make_user


def _worker(db, *, name, ward, active=True, pending=False):
    # `make_user` allocates the phone; these tests only care about the three
    # flags that decide which population a worker belongs to.
    return make_user(
        db,
        role="worker",
        name=name,
        ward_id=ward.id,
        is_active=active,
        must_change_password=pending,
    )


def _names(payload):
    return {w["name"] for w in payload["items"]}


class TestPendingInviteFilter:
    def test_selects_only_workers_who_have_not_signed_in(self, client, db, hierarchy, ward_admin):
        ward = hierarchy["ward"]
        _worker(db, name="Signed In", ward=ward)
        _worker(db, name="Never Signed In", ward=ward, pending=True)
        _worker(db, name="Deactivated", ward=ward, active=False)
        db.commit()

        r = client.get(
            "/admin/workers", params={"pending_invite": True}, headers=auth_header(ward_admin)
        )

        assert r.status_code == 200
        assert _names(r.json()) == {"Never Signed In"}

    def test_deactivated_worker_is_never_pending_even_holding_a_temp_password(
        self, client, db, hierarchy, ward_admin
    ):
        """The case that makes this more than a label change.

        A worker invited and then deactivated before signing in still carries
        ``must_change_password``. Offering to re-send would mint a working
        sign-in link for a disabled account, so the filter requires both.
        """
        ward = hierarchy["ward"]
        _worker(db, name="Invited Then Disabled", ward=ward, active=False, pending=True)
        db.commit()

        r = client.get(
            "/admin/workers", params={"pending_invite": True}, headers=auth_header(ward_admin)
        )

        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_false_selects_workers_who_have_signed_in(self, client, db, hierarchy, ward_admin):
        ward = hierarchy["ward"]
        _worker(db, name="Signed In", ward=ward)
        _worker(db, name="Never Signed In", ward=ward, pending=True)
        db.commit()

        r = client.get(
            "/admin/workers", params={"pending_invite": False}, headers=auth_header(ward_admin)
        )

        assert r.status_code == 200
        assert _names(r.json()) == {"Signed In"}

    def test_omitting_it_keeps_the_previous_default(self, client, db, hierarchy, ward_admin):
        """Active-only, both signed-in and pending — unchanged behaviour."""
        ward = hierarchy["ward"]
        _worker(db, name="Signed In", ward=ward)
        _worker(db, name="Never Signed In", ward=ward, pending=True)
        _worker(db, name="Deactivated", ward=ward, active=False)
        db.commit()

        r = client.get("/admin/workers", headers=auth_header(ward_admin))

        assert r.status_code == 200
        assert _names(r.json()) == {"Signed In", "Never Signed In"}

    def test_is_active_false_still_means_deactivated(self, client, db, hierarchy, ward_admin):
        """The filter the tab used to rely on is untouched — it just is not the
        same question as "who was invited"."""
        ward = hierarchy["ward"]
        _worker(db, name="Never Signed In", ward=ward, pending=True)
        _worker(db, name="Deactivated", ward=ward, active=False)
        db.commit()

        r = client.get(
            "/admin/workers", params={"is_active": False}, headers=auth_header(ward_admin)
        )

        assert r.status_code == 200
        assert _names(r.json()) == {"Deactivated"}

    def test_respects_jurisdiction(self, client, db, hierarchy, ward_admin):
        """The filter composes with scoping rather than bypassing it — a ward
        admin must not see another ward's outstanding invitations."""
        _worker(db, name="Mine", ward=hierarchy["ward"], pending=True)
        _worker(db, name="Theirs", ward=hierarchy["other_ward"], pending=True)
        db.commit()

        r = client.get(
            "/admin/workers", params={"pending_invite": True}, headers=auth_header(ward_admin)
        )

        assert r.status_code == 200
        assert _names(r.json()) == {"Mine"}

    def test_combines_with_search(self, client, db, hierarchy, ward_admin):
        ward = hierarchy["ward"]
        _worker(db, name="Asha Patel", ward=ward, pending=True)
        _worker(db, name="Bhavin Shah", ward=ward, pending=True)
        db.commit()

        r = client.get(
            "/admin/workers",
            params={"pending_invite": True, "search": "Asha"},
            headers=auth_header(ward_admin),
        )

        assert r.status_code == 200
        assert _names(r.json()) == {"Asha Patel"}
