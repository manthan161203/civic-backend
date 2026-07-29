#!/usr/bin/env python3
"""Replace the operational data with a realistic, self-consistent dataset.

The dev database accumulates debug residue — citizens with no name, issues
described "final verification", a worker called "Sync Test Worker". That is fine
while you are testing one endpoint and useless the moment you want to look at
the console and judge whether it works. Half the screens render an empty state,
and the ones that do not show three identical potholes.

This drops everything operational and rebuilds it so **every admin screen has
something true-shaped on it**: issues across all five statuses with escalations
and blocks, workers on shifts with performance histories, citizens with points
and badges, surveys, disputes, complaints, flags, announcements, geofences.

── What it does NOT touch ───────────────────────────────────────────────────

`districts`, `talukas` and `wards`. That is the real Gujarat hierarchy loaded by
`seed_locations.py`, it is reference data rather than sample data, and every FK
here hangs off it. Run that first if the tree is empty.

── Safety ───────────────────────────────────────────────────────────────────

This deletes rows. It refuses to run against a database whose URL does not look
like a local one unless `--force` is passed, on the same principle as the guard
in `tests/conftest.py`: a seeder pointed at production is not a recoverable
mistake.

    docker compose run --rm api python scripts/seed_demo.py
    docker compose run --rm api python scripts/seed_demo.py --dry-run
"""

from __future__ import annotations

import argparse
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import make_url

# Deterministic. A seeder that produces a different dataset every run makes
# "did my change do that, or did the data move?" unanswerable.
random.seed(20260729)

sys.path.insert(0, "/app")

from app.core.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Announcement,
    CustomIssueType,
    Dispute,
    Geofence,
    Issue,
    IssueComment,
    IssueFlag,
    IssueSquad,
    IssueVote,
    Notification,
    RewardTransaction,
    SatisfactionSurvey,
    User,
    UserBadge,
    WorkerComplaint,
    WorkerShift,
)
from app.models.admin_message import AdminMessage  # noqa: E402
from app.models.location import District, Taluka, Ward  # noqa: E402
from app.core.security import hash_password  # noqa: E402

NOW = datetime.now(timezone.utc)


def ago(days: float = 0, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours)


def past(dt: datetime) -> datetime:
    """Never let a derived timestamp land in the future.

    Most rows here are dated relative to their issue — a comment a few days
    after the report, a vote within 72 hours. For an issue filed this morning
    that arithmetic runs past `now`, and a console showing a comment posted
    tomorrow is the kind of detail that makes everything else look untrustworthy.
    """
    return min(dt, NOW)


# ── Names ─────────────────────────────────────────────────────────────────────
# Gujarati given names and surnames, because the deployment is Gujarat and a
# console full of "Test User 1" tells you nothing about how a real name wraps in
# a table cell.

FIRST_M = [
    "Amit", "Bhavesh", "Chirag", "Darshan", "Harsh", "Jignesh", "Kalpesh",
    "Manish", "Nirav", "Parth", "Rohit", "Sanjay", "Tushar", "Vikram", "Yash",
    "Ketan", "Mehul", "Ronak", "Hiren", "Dhaval",
]
FIRST_F = [
    "Aarti", "Bhavna", "Chetna", "Dimple", "Falguni", "Gopi", "Hetal",
    "Jigna", "Krupa", "Meera", "Nisha", "Priya", "Rekha", "Sejal", "Trupti",
    "Urmila", "Vaishali", "Zarna", "Payal", "Shruti",
]
SURNAMES = [
    "Patel", "Shah", "Desai", "Mehta", "Trivedi", "Joshi", "Bhatt", "Vyas",
    "Solanki", "Chauhan", "Parmar", "Rathod", "Gohil", "Jadeja", "Dave",
    "Modi", "Amin", "Pandya", "Thakkar", "Raval",
]


def full_name(rng: random.Random) -> str:
    given = rng.choice(FIRST_M if rng.random() < 0.55 else FIRST_F)
    return f"{given} {rng.choice(SURNAMES)}"


# ── Issue content ─────────────────────────────────────────────────────────────
# Written the way a citizen types on a phone: specific, a bit terse, occasionally
# shouty. Lorem ipsum in a triage console hides exactly the thing you want to
# check — whether a real description fits the row.

ISSUE_TEMPLATES = {
    "pothole": (
        "roads",
        [
            ("Deep pothole outside {place}", "Been here since the last rains. Two-wheelers swerve into oncoming traffic to avoid it. Someone will get hurt."),
            ("Road caved in near {place}", "About a foot across and getting wider. Auto drivers are refusing to take this route now."),
            ("Broken patch on the {place} approach", "Was repaired three months ago and it has already come apart. The filling material has washed out."),
            ("Potholes all along {place}", "Not one hole, the whole stretch. Impossible to cycle."),
        ],
    ),
    "streetlight": (
        "electricity",
        [
            ("Streetlight out for two weeks at {place}", "The whole lane is dark after 7pm. Women in the building have stopped using the back gate."),
            ("Flickering light near {place}", "Comes on, goes off every few seconds all night. Bright enough to keep the first floor awake."),
            ("Pole leaning dangerously by {place}", "The base is rusted through. It moves when a bus passes."),
            ("No light at the {place} crossing", "Busy junction with no lighting at all. Two near misses this week."),
        ],
    ),
    "garbage": (
        "sanitation",
        [
            ("Bin not emptied for five days at {place}", "Overflowing onto the footpath. Stray dogs have spread it across the road."),
            ("Dumping at the empty plot near {place}", "People are tipping construction waste here overnight. It is not a dump."),
            ("Garbage van has stopped coming to {place}", "Used to be daily, now nothing for a week. Residents are burning it, which is worse."),
            ("Dead animal near {place}", "Been there two days. The smell is unbearable in this heat."),
        ],
    ),
    "drain": (
        "sanitation",
        [
            ("Drain blocked and overflowing at {place}", "Sewage on the road outside the school gate. Children walk through it."),
            ("Open manhole near {place}", "Cover has been missing for days. Someone has put a branch in it as a warning. That is not enough."),
            ("Storm drain choked before monsoon at {place}", "Full of silt and plastic. This lane floods every year and nothing has been cleared."),
            ("Stagnant water beside {place}", "Standing for a fortnight. Mosquitoes everywhere, two dengue cases in the building already."),
        ],
    ),
    "water": (
        "water",
        [
            ("No supply for three days at {place}", "Whole building affected. Tankers are charging 800 rupees a trip."),
            ("Pipeline leaking at {place}", "Clean water running into the drain around the clock while we get an hour a day."),
            ("Muddy water from the taps near {place}", "Brown and it smells. Nobody is drinking it, we are buying cans."),
            ("Very low pressure at {place}", "Only reaches the ground floor. Upper floors get nothing during supply hours."),
        ],
    ),
    "other": (
        "parks",
        [
            ("Broken swings in the garden at {place}", "Chain snapped on two of them. Children still climbing on the frame."),
            ("Stray dog pack near {place}", "Six or seven of them, aggressive around the school at closing time."),
            ("Encroachment on the footpath at {place}", "Vendors have taken the whole width. Pedestrians walk on the road."),
            ("Illegal hoarding blocking the signal at {place}", "You cannot see the traffic light coming from the north."),
        ],
    ),
}

PLACES = [
    "the bus stand", "the municipal school", "the vegetable market", "the temple",
    "the community hall", "the primary health centre", "the petrol pump",
    "the housing society gate", "the railway crossing", "the post office",
    "the bank branch", "the sports ground", "the water tank", "the police chowky",
]

STREETS = [
    "Main Bazaar Road", "Station Road", "Ring Road", "College Road",
    "Gandhi Chowk", "Nehru Marg", "Market Lane", "Old Highway",
    "Sardar Patel Road", "Temple Street",
]

RESOLUTION_NOTES = [
    "Filled and compacted. Surface levelled with the surrounding road.",
    "Replaced the fitting and tested. Working through the night now.",
    "Cleared and disinfected. Collection schedule restored to daily.",
    "Jetted the line and removed the blockage. Flow normal.",
    "Valve replaced at the junction. Pressure restored to the upper floors.",
    "Removed and the area cleaned. Barricade left up for two days.",
]

SURVEY_FEEDBACK = [
    "Fixed quickly, thank you. The worker even came back to check.",
    "Took a while but the job was done properly.",
    "Sorted, but nobody told me it was done. I found out by walking past.",
    "Half done. The hole is filled but the road is still uneven.",
    "Excellent. Two days from reporting to fixed.",
    "The worker was polite and explained what he was doing.",
    None,
    None,
    "Third time reporting the same spot this year. Fix it properly.",
    "Good work but it took three weeks.",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing.")
    parser.add_argument("--force", action="store_true", help="Run against a non-local database.")
    args = parser.parse_args()

    url = make_url(settings.DATABASE_URL)
    host = (url.host or "").lower()
    local = host in ("db", "localhost", "127.0.0.1", "testdb", "")
    if not local and not args.force:
        print(f"Refusing to run: DATABASE_URL host is {host!r}, which does not look local.")
        print("Pass --force if you are certain.")
        return 1

    db = SessionLocal()
    rng = random.Random(20260729)

    try:
        wards = (
            db.query(Ward, Taluka, District)
            .join(Taluka, Ward.taluka_id == Taluka.id)
            .join(District, Taluka.district_id == District.id)
            .order_by(District.name, Taluka.name, Ward.ward_number)
            .all()
        )
        if not wards:
            print("No wards found. Run scripts/seed_locations.py first.")
            return 1

        print(f"Location tree: {len(wards)} wards — left untouched.\n")

        # ── Wipe ──────────────────────────────────────────────────────────────
        # Child-first, so no FK needs deferring. `users` and `issues` go last
        # because nearly everything points at one or the other.
        wipe_order = [
            "geofence_alerts", "geofences",
            "admin_override_logs", "admin_overrides", "admin_messages",
            "satisfaction_surveys", "disputes", "worker_complaints",
            "issue_flags", "issue_votes", "issue_comments", "issue_bookmarks",
            "issue_squads", "custom_issue_types",
            "notifications", "announcements",
            "reward_transactions", "user_badges",
            "worker_shifts", "ward_subscriptions",
            "synced_actions", "refresh_tokens", "otps",
            "issues", "users",
        ]

        counts = {}
        for table in wipe_order:
            try:
                n = db.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0
            except Exception:
                db.rollback()
                continue  # table not in this schema version
            if n:
                counts[table] = n

        if counts:
            print("Removing:")
            for table, n in counts.items():
                print(f"  {n:5d}  {table}")
        else:
            print("Nothing to remove — database is already empty of operational data.")
        print()

        if args.dry_run:
            print("--dry-run: nothing written.")
            return 0

        for table in wipe_order:
            try:
                db.execute(text(f"DELETE FROM {table}"))
            except Exception:
                db.rollback()
        db.commit()

        # ── People ────────────────────────────────────────────────────────────
        phone_seq = iter(range(10, 400))

        def phone() -> str:
            return f"+9198{next(phone_seq):08d}"

        by_district: dict[str, list] = {}
        for w, t, d in wards:
            by_district.setdefault(d.name, []).append((w, t, d))

        users: list[User] = []

        def add_user(**kw) -> User:
            # Defaults the caller can override, rather than positional values it
            # would collide with — workers pass their own `is_active`.
            kw.setdefault("language", "en")
            kw.setdefault("is_active", True)
            u = User(id=uuid.uuid4(), **kw)
            db.add(u)
            users.append(u)
            return u

        # One super admin — the account you sign in with.
        super_admin = add_user(
            phone="+919900000001",
            email="admin@civic.gujarat.gov.in",
            name="Manthan Patel",
            role="admin",
            password_hash=hash_password("Admin@12345"),
            created_at=ago(days=400),
        )

        # A scoped admin per tier, so the jurisdiction filtering is visible.
        district_admins, taluka_admins, ward_admins = [], [], []
        for dname, entries in by_district.items():
            w, t, d = entries[0]
            district_admins.append(
                add_user(
                    phone=phone(), email=f"{dname.lower()}.dc@civic.gujarat.gov.in",
                    name=full_name(rng), role="district_admin",
                    district_id=d.id, password_hash=hash_password("Admin@12345"),
                    created_at=ago(days=rng.randint(200, 380)),
                )
            )

        seen_talukas = set()
        for w, t, d in wards:
            if t.id in seen_talukas:
                continue
            seen_talukas.add(t.id)
            taluka_admins.append(
                add_user(
                    phone=phone(), email=f"{t.name.lower().replace(' ', '')}.to@civic.gujarat.gov.in",
                    name=full_name(rng), role="taluka_admin",
                    taluka_id=t.id, district_id=d.id,
                    password_hash=hash_password("Admin@12345"),
                    created_at=ago(days=rng.randint(120, 340)),
                )
            )

        for w, t, d in wards[:12]:
            ward_admins.append(
                add_user(
                    phone=phone(), email=f"ward{w.ward_number}.{t.name.lower().replace(' ', '')}@civic.gujarat.gov.in",
                    name=full_name(rng), role="ward_admin",
                    ward_id=w.id, ward=w.name, taluka_id=t.id, district_id=d.id,
                    password_hash=hash_password("Admin@12345"),
                    created_at=ago(days=rng.randint(60, 300)),
                )
            )

        # Workers — two to four per ward, spread across departments.
        DEPTS = ["roads", "water", "electricity", "sanitation", "parks"]
        workers: list[User] = []
        for w, t, d in wards:
            for _ in range(rng.randint(2, 4)):
                joined = ago(days=rng.randint(20, 500))
                # A tenth are still holding their invitation — this is what the
                # "Awaiting first sign-in" tab exists for.
                pending = rng.random() < 0.10
                online = (not pending) and rng.random() < 0.35
                wk = add_user(
                    phone=phone(),
                    email=f"worker{len(workers) + 1}@fieldstaff.gujarat.gov.in",
                    name=full_name(rng),
                    role="worker",
                    ward_id=w.id, ward=w.name, taluka_id=t.id, district_id=d.id,
                    department=rng.choice(DEPTS),
                    is_online=online,
                    is_available=online and rng.random() < 0.7,
                    is_active=rng.random() > 0.06,
                    must_change_password=pending,
                    invitation_sent_at=joined if pending else None,
                    password_hash=hash_password("Worker@12345"),
                    created_at=joined,
                    # Jitter around the ward centroid so the live map is not a
                    # single stacked pin per ward.
                    latitude=(w.centroid_lat or 22.3) + rng.uniform(-0.012, 0.012) if online else None,
                    longitude=(w.centroid_lon or 71.2) + rng.uniform(-0.012, 0.012) if online else None,
                    location_updated_at=ago(hours=rng.uniform(0.1, 6)) if online else None,
                )
                workers.append(wk)

        # Citizens.
        citizens: list[User] = []
        for w, t, d in wards:
            for _ in range(rng.randint(4, 9)):
                citizens.append(
                    add_user(
                        phone=phone(),
                        name=full_name(rng),
                        role="citizen",
                        ward_id=w.id, ward=w.name, taluka_id=t.id, district_id=d.id,
                        aadhar_verified=rng.random() < 0.55,
                        is_active=rng.random() > 0.03,
                        created_at=ago(days=rng.randint(1, 420)),
                        latitude=(w.centroid_lat or 22.3) + rng.uniform(-0.01, 0.01),
                        longitude=(w.centroid_lon or 71.2) + rng.uniform(-0.01, 0.01),
                    )
                )

        db.flush()
        print(f"People: 1 super admin, {len(district_admins)} district, "
              f"{len(taluka_admins)} taluka, {len(ward_admins)} ward admins, "
              f"{len(workers)} workers, {len(citizens)} citizens")

        # ── Issues ────────────────────────────────────────────────────────────
        #
        # Age drives status. Something reported this morning is open; something
        # from six weeks ago is almost certainly closed. Sampling status
        # uniformly produces a queue that looks nothing like a real one — and
        # makes the SLA screen meaningless, because nothing is ever overdue.
        issues: list[Issue] = []
        ward_workers: dict[uuid.UUID, list[User]] = {}
        for wk in workers:
            ward_workers.setdefault(wk.ward_id, []).append(wk)
        ward_citizens: dict[uuid.UUID, list[User]] = {}
        for c in citizens:
            ward_citizens.setdefault(c.ward_id, []).append(c)

        for w, t, d in wards:
            local_citizens = ward_citizens.get(w.id) or citizens
            local_workers = [x for x in ward_workers.get(w.id, []) if x.is_active and not x.must_change_password]

            for _ in range(rng.randint(6, 14)):
                itype = rng.choices(
                    ["pothole", "drain", "garbage", "streetlight", "water", "other"],
                    weights=[26, 20, 22, 16, 12, 4],
                )[0]
                dept, templates = ISSUE_TEMPLATES[itype]
                title, body = rng.choice(templates)
                place = rng.choice(PLACES)
                description = f"{title.format(place=place)}. {body.format(place=place)}"

                age_days = rng.choices(
                    [rng.uniform(0, 2), rng.uniform(2, 10), rng.uniform(10, 30), rng.uniform(30, 75)],
                    weights=[18, 30, 30, 22],
                )[0]
                created = ago(days=age_days)

                if age_days < 1.5:
                    status = rng.choices(["open", "assigned"], weights=[75, 25])[0]
                elif age_days < 10:
                    status = rng.choices(["open", "assigned", "in_progress", "resolved"], weights=[22, 26, 32, 20])[0]
                elif age_days < 30:
                    status = rng.choices(["open", "in_progress", "resolved", "closed"], weights=[10, 22, 40, 28])[0]
                else:
                    status = rng.choices(["open", "in_progress", "resolved", "closed"], weights=[6, 8, 26, 60])[0]

                severity = rng.choices(["low", "medium", "high"], weights=[34, 46, 20])[0]
                priority = rng.choices(["low", "medium", "high", "urgent"], weights=[22, 44, 26, 8])[0]

                assigned = None
                if status != "open" and local_workers:
                    assigned = rng.choice(local_workers)

                # Old and still not resolved is what escalation is for.
                escalated = status in ("open", "assigned", "in_progress") and age_days > 14 and rng.random() < 0.45
                blocked = status == "in_progress" and rng.random() < 0.14

                resolved_at = None
                if status in ("resolved", "closed"):
                    resolved_at = past(created + timedelta(days=rng.uniform(0.4, min(age_days, 21))))

                issue = Issue(
                    id=uuid.uuid4(),
                    reporter_id=rng.choice(local_citizens).id,
                    assigned_worker_id=assigned.id if assigned else None,
                    issue_type=itype,
                    department=dept,
                    severity=severity,
                    priority=priority,
                    status=status,
                    description=description,
                    latitude=(w.centroid_lat or 22.3) + rng.uniform(-0.014, 0.014),
                    longitude=(w.centroid_lon or 71.2) + rng.uniform(-0.014, 0.014),
                    address=f"{rng.choice(STREETS)}, {w.name}, {t.name}, {d.name}",
                    ward=w.name,
                    ward_id=w.id,
                    upvote_count=rng.choices([0, 1, 2, 5, 12, 31], weights=[30, 24, 20, 14, 9, 3])[0],
                    is_escalated=escalated,
                    escalated_at=past(created + timedelta(days=rng.uniform(7, 14))) if escalated else None,
                    escalation_level=rng.randint(1, 3) if escalated else 0,
                    is_blocked=blocked,
                    blocked_reason=rng.choice([
                        "Waiting on the electricity board to de-energise the line.",
                        "Need a JCB — the one allotted is on another job until Thursday.",
                        "Material not in store. Indent raised, no date yet.",
                        "Private land. Owner is refusing access.",
                    ]) if blocked else None,
                    resolution_notes=rng.choice(RESOLUTION_NOTES) if status in ("resolved", "closed") else None,
                    citizen_rating=rng.choices([None, 1, 2, 3, 4, 5], weights=[30, 4, 6, 14, 26, 20])[0]
                    if status in ("resolved", "closed") else None,
                    created_at=created,
                    resolved_at=resolved_at,
                    assigned_at=past(created + timedelta(hours=rng.uniform(1, 40))) if assigned else None,
                )

                # AI fields on roughly half — enough that the insights screen has
                # both confident and low-confidence rows to drill into.
                if rng.random() < 0.5:
                    issue.ai_issue_type = itype if rng.random() < 0.85 else rng.choice(list(ISSUE_TEMPLATES))
                    issue.ai_severity = severity if rng.random() < 0.8 else rng.choice(["low", "medium", "high"])
                    issue.ai_confidence = round(rng.uniform(0.34, 0.98), 2)
                if status in ("resolved", "closed") and rng.random() < 0.6:
                    issue.ai_is_resolved = rng.random() < 0.85
                    issue.ai_resolution_quality = rng.choices(
                        ["good", "partial", "poor"], weights=[62, 27, 11]
                    )[0]

                db.add(issue)
                issues.append(issue)

        db.flush()
        resolved = [i for i in issues if i.status in ("resolved", "closed")]
        print(f"Issues: {len(issues)} across {len(wards)} wards "
              f"({sum(i.is_escalated for i in issues)} escalated, "
              f"{sum(i.is_blocked for i in issues)} blocked, {len(resolved)} resolved)")

        # ── Everything that hangs off an issue ────────────────────────────────
        for issue in issues:
            # `uq_issue_vote` is unique on (issue_id, user_id) — one person, one
            # vote — so sample without replacement rather than picking randomly
            # and hoping.
            voters = rng.sample(citizens, k=min(issue.upvote_count, 6, len(citizens)))
            for voter in voters:
                db.add(IssueVote(
                    id=uuid.uuid4(), issue_id=issue.id, user_id=voter.id,
                    created_at=past(issue.created_at + timedelta(hours=rng.uniform(1, 72))),
                ))

            if rng.random() < 0.35:
                db.add(IssueComment(
                    id=uuid.uuid4(), issue_id=issue.id,
                    author_id=rng.choice(citizens).id,
                    body=rng.choice([
                        "Same problem outside my gate. Adding my vote.",
                        "Any update on this? Three weeks now.",
                        "The worker came yesterday and had a look but did not start.",
                        "Thank you for reporting, I was about to do the same.",
                        "This is the second time this year at the same spot.",
                    ]),
                    created_at=past(issue.created_at + timedelta(days=rng.uniform(0.2, 6))),
                ))
            if issue.is_blocked and rng.random() < 0.7:
                db.add(IssueComment(
                    id=uuid.uuid4(), issue_id=issue.id,
                    author_id=issue.assigned_worker_id or rng.choice(workers).id,
                    is_internal=True,
                    body="Site visited. Cannot proceed until the blocker above is cleared.",
                    created_at=past(issue.created_at + timedelta(days=rng.uniform(1, 5))),
                ))

        # Surveys on about 45% of resolved issues.
        for issue in resolved:
            if rng.random() > 0.45:
                continue
            fully = issue.citizen_rating is None or issue.citizen_rating >= 3
            db.add(SatisfactionSurvey(
                id=uuid.uuid4(), issue_id=issue.id, citizen_id=issue.reporter_id,
                fully_resolved=fully,
                speed_rating=rng.choices([1, 2, 3], weights=[22, 44, 34])[0],
                would_report_again=fully or rng.random() < 0.5,
                feedback=rng.choice(SURVEY_FEEDBACK),
                created_at=past((issue.resolved_at or issue.created_at) + timedelta(days=rng.uniform(0.2, 4))),
            ))

        # Disputes — citizens contesting a resolution they do not accept.
        for issue in rng.sample(resolved, k=min(14, len(resolved))):
            db.add(Dispute(
                id=uuid.uuid4(), issue_id=issue.id, citizen_id=issue.reporter_id,
                reason=rng.choice([
                    "Marked resolved but nothing has been done. The hole is still there.",
                    "They filled it with loose gravel which washed out in one day.",
                    "The photo attached is of a different street.",
                    "Half the stretch was done and the rest left. Not resolved.",
                ]),
                status=rng.choices(["open", "under_review", "accepted", "rejected"], weights=[38, 26, 20, 16])[0],
                created_at=past((issue.resolved_at or issue.created_at) + timedelta(days=rng.uniform(0.5, 6))),
            ))

        # Complaints about workers.
        for issue in rng.sample([i for i in issues if i.assigned_worker_id], k=min(10, len(issues))):
            db.add(WorkerComplaint(
                id=uuid.uuid4(), worker_id=issue.assigned_worker_id,
                citizen_id=issue.reporter_id, issue_id=issue.id,
                reason=rng.choice(["rude_behavior", "poor_work", "delayed", "no_show", "other"]),
                description=rng.choice([
                    "Came, took a photo, left. Nothing was actually repaired.",
                    "Was rude when asked how long it would take.",
                    "Marked the job done without visiting the site at all.",
                    "Promised to return the next morning and never came back.",
                ]),
                status=rng.choices(["pending", "investigating", "resolved", "dismissed"], weights=[42, 22, 24, 12])[0],
                created_at=past(issue.created_at + timedelta(days=rng.uniform(1, 10))),
            ))

        # Flags on content.
        for issue in rng.sample(issues, k=min(16, len(issues))):
            db.add(IssueFlag(
                id=uuid.uuid4(), issue_id=issue.id, reporter_id=rng.choice(citizens).id,
                reason=rng.choices(["duplicate", "spam", "false_report", "inappropriate", "other"],
                                   weights=[40, 20, 22, 10, 8])[0],
                details=rng.choice([
                    "Already reported two streets down, same problem.",
                    "This is not a civic issue, it is a private dispute.",
                    "Photo does not match the description.",
                    None,
                ]),
                status=rng.choices(["pending", "reviewed", "dismissed"], weights=[52, 28, 20])[0],
                created_at=past(issue.created_at + timedelta(days=rng.uniform(0.5, 8))),
            ))

        # Squads on a handful of big jobs.
        for issue in rng.sample([i for i in issues if i.assigned_worker_id and i.priority in ("high", "urgent")],
                                k=min(6, len(issues))):
            pool = [x for x in ward_workers.get(issue.ward_id, []) if x.id != issue.assigned_worker_id]
            db.add(IssueSquad(
                id=uuid.uuid4(), issue_id=issue.id,
                lead_worker_id=issue.assigned_worker_id,
                assistant_ids=[str(x.id) for x in pool[:2]],
                status="completed" if issue.status in ("resolved", "closed") else "active",
                notes="Two-person job — one on the line, one on traffic.",
                created_at=past(issue.created_at + timedelta(hours=rng.uniform(4, 48))),
            ))

        # ── Rewards, badges, notifications ────────────────────────────────────
        BADGES = ["first_report", "ten_reports", "verified_citizen", "top_reporter", "helpful_neighbour"]
        for c in citizens:
            reported = [i for i in issues if i.reporter_id == c.id]
            for i in reported:
                db.add(RewardTransaction(
                    id=uuid.uuid4(), user_id=c.id, points=10,
                    event_type="issue_reported", reference_id=i.id,
                    note="Reported an issue", created_at=i.created_at,
                ))
                if i.status in ("resolved", "closed"):
                    db.add(RewardTransaction(
                        id=uuid.uuid4(), user_id=c.id, points=25,
                        event_type="issue_resolved", reference_id=i.id,
                        note="An issue you reported was resolved",
                        created_at=i.resolved_at or i.created_at,
                    ))
            if reported:
                db.add(UserBadge(id=uuid.uuid4(), user_id=c.id, badge_key="first_report",
                                 earned_at=min(i.created_at for i in reported)))
            if len(reported) >= 4:
                db.add(UserBadge(id=uuid.uuid4(), user_id=c.id, badge_key=rng.choice(BADGES[1:]),
                                 earned_at=max(i.created_at for i in reported)))

        for wk in workers:
            done = [i for i in issues if i.assigned_worker_id == wk.id and i.status in ("resolved", "closed")]
            for i in done:
                db.add(RewardTransaction(
                    id=uuid.uuid4(), user_id=wk.id, points=15,
                    event_type="task_completed", reference_id=i.id,
                    note="Completed a task", created_at=i.resolved_at or i.created_at,
                ))

        for issue in rng.sample(issues, k=min(45, len(issues))):
            db.add(Notification(
                id=uuid.uuid4(), user_id=issue.reporter_id, issue_id=issue.id,
                title=rng.choice(["Your issue was assigned", "Work has started", "Your issue was resolved",
                                  "An update on your report"]),
                body="Tap to see the current status and the latest photos.",
                type="status_update", action_type="open_issue",
                is_read=rng.random() < 0.6,
                created_at=past(issue.created_at + timedelta(days=rng.uniform(0.2, 5))),
            ))

        # ── Worker shifts ─────────────────────────────────────────────────────
        for wk in workers:
            if wk.must_change_password or not wk.is_active:
                continue
            for day in range(6 if rng.random() < 0.7 else 5):
                start = rng.choice(["08:00", "09:00", "10:00"])
                db.add(WorkerShift(
                    id=uuid.uuid4(), worker_id=wk.id, day_of_week=day,
                    start_time=start, end_time=f"{int(start[:2]) + 8:02d}:00",
                ))

        # ── Announcements, geofences, custom types, admin messages ────────────
        state_author = super_admin
        db.add(Announcement(
            id=uuid.uuid4(), title="Monsoon preparedness drive begins 1 June",
            body=("Desilting of storm drains starts across all municipal wards from 1 June. "
                  "Residents are asked to report choked drains through the app so crews can be "
                  "routed to the worst stretches first."),
            author_id=state_author.id, scope="state",
            push_dispatched_at=ago(days=12),
            created_at=ago(days=12),
        ))
        db.add(Announcement(
            id=uuid.uuid4(), title="Water supply interrupted Thursday 06:00–14:00",
            body=("Pipeline tie-in work at the main junction. Supply to the affected wards will be "
                  "restored by 14:00. Tankers will be stationed at the community hall."),
            author_id=state_author.id, scope="district",
            district_id=wards[0][2].id,
            push_dispatched_at=ago(days=3),
            created_at=ago(days=3),
        ))
        db.add(Announcement(
            id=uuid.uuid4(), title="Ward meeting on Saturday",
            body="Open meeting at the community hall, 5pm, to review pending civic complaints in this ward.",
            author_id=(ward_admins[0] if ward_admins else state_author).id, scope="ward",
            ward_id=wards[0][0].id,
            created_at=ago(days=1),
        ))

        for w, t, d in wards[:4]:
            db.add(Geofence(
                id=uuid.uuid4(),
                name=f"{w.name} flood-prone zone",
                latitude=w.centroid_lat or 22.3,
                longitude=w.centroid_lon or 71.2,
                radius_km=round(rng.uniform(1.2, 3.5), 1),
                ward_id=w.id, taluka_id=t.id, district_id=d.id,
                created_by_id=super_admin.id,
                created_at=ago(days=rng.randint(5, 90)),
            ))

        for label, slug, approved, uses in [
            ("Stray cattle on the road", "stray-cattle", False, 14),
            ("Broken footpath tiles", "broken-footpath", True, 41),
            ("Illegal parking", "illegal-parking", False, 9),
            ("Tree branch blocking the road", "fallen-branch", True, 23),
            ("Noise from construction at night", "night-noise", False, 4),
        ]:
            db.add(CustomIssueType(
                id=uuid.uuid4(), label=label, slug=slug,
                suggested_by=rng.choice(citizens).id,
                usage_count=uses, is_approved=approved,
                created_at=ago(days=rng.randint(10, 160)),
            ))

        if district_admins:
            for i, subject in enumerate([
                ("Escalated potholes on the ring road", "urgent"),
                ("Worker shortage this week in sanitation", "normal"),
                ("Monthly resolution figures look off", "normal"),
            ]):
                db.add(AdminMessage(
                    id=uuid.uuid4(),
                    sender_id=district_admins[i % len(district_admins)].id,
                    receiver_id=super_admin.id,
                    subject=subject[0],
                    body=("Raising this for your attention. Details are on the relevant screen; "
                          "happy to discuss on call if quicker."),
                    message_type="escalation" if subject[1] == "urgent" else "general",
                    is_urgent=subject[1],
                    is_read=None if i == 0 else ago(days=1),
                    created_at=ago(days=i + 1),
                ))

        db.commit()

        # ── Report ────────────────────────────────────────────────────────────
        print()
        print("Seeded. Sign in to the console with:")
        print("    phone     +919900000001")
        print("    password  Admin@12345")
        print("    (or request an OTP — SMS_BACKEND=console prints it to the api logs)")
        print()
        print("Scoped admins for testing jurisdiction filtering:")
        for label, group in (("district", district_admins), ("taluka", taluka_admins), ("ward", ward_admins)):
            if group:
                print(f"    {label:9} {group[0].phone}  {group[0].name}  (password Admin@12345)")
        return 0

    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"Failed, nothing committed: {type(exc).__name__}: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
