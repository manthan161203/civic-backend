#!/usr/bin/env python3
"""
Seed the District -> Taluka -> Ward hierarchy from a CSV.
=========================================================

The location tree has no seed data of any kind, and a fresh database starts
empty. Citizens can still report issues (``ward_id`` is optional), but the
hierarchy-scoped admin roles cannot work without it: ``get_admin_scope_filter``
in ``app/core/deps.py`` returns an empty filter for a ``ward_admin`` whose
``ward_id`` is NULL, which silently widens their view to every issue in the
system rather than narrowing it.

Centroids are read from the CSV rather than geocoded. The admin API falls back
to live Nominatim lookups, which is fine for adding one ward by hand but wrong
for a bootstrap path: it is rate-limited, it needs outbound internet, and a
failure leaves the centroid NULL, which breaks distance-based worker routing.

Usage::

    docker compose run --rm api python scripts/seed_locations.py
    docker compose run --rm api python scripts/seed_locations.py --file scripts/my.csv
    docker compose run --rm api python scripts/seed_locations.py --dry-run

Idempotent: rows that already exist are left alone, so it is safe to re-run
after extending the CSV. Nothing is ever deleted or renamed.

CSV columns:
    district, district_lat, district_lon,
    taluka,   taluka_lat,   taluka_lon,
    ward_number, ward, ward_lat, ward_lon
"""

import argparse
import csv
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logger import get_logger  # noqa: E402
from app.database import SessionLocal, check_db_connection  # noqa: E402
from app.models.location import District, Taluka, Ward  # noqa: E402

logger = get_logger("seed_locations")

DEFAULT_CSV = Path(__file__).resolve().parent / "locations_gujarat.csv"
REQUIRED_COLUMNS = {
    "district", "district_lat", "district_lon",
    "taluka", "taluka_lat", "taluka_lon",
    "ward_number", "ward", "ward_lat", "ward_lon",
}


def _float(row: dict, key: str) -> float | None:
    """Parse an optional float column, treating blanks as absent."""
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"Row {row}: '{key}' is not a number: {raw!r}")


def seed(csv_path: Path, state_name: str, dry_run: bool) -> int:
    """Insert any missing districts, talukas and wards from the CSV.

    Args:
        csv_path:   CSV file to read.
        state_name: State the districts belong to.
        dry_run:    Report what would change without writing.

    Returns:
        Process exit code.
    """
    if not csv_path.exists():
        logger.error("CSV not found: %s", csv_path)
        return 1

    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    if not rows:
        logger.error("CSV is empty: %s", csv_path)
        return 1

    missing = REQUIRED_COLUMNS - set(rows[0].keys())
    if missing:
        logger.error("CSV is missing columns: %s", ", ".join(sorted(missing)))
        return 1

    if not check_db_connection():
        logger.error("Database is unreachable")
        return 1

    db = SessionLocal()
    created = {"district": 0, "taluka": 0, "ward": 0}
    skipped = {"district": 0, "taluka": 0, "ward": 0}

    try:
        for row in rows:
            d_name = row["district"].strip()
            t_name = row["taluka"].strip()
            w_name = row["ward"].strip()
            if not (d_name and t_name and w_name):
                logger.warning("Skipping row with a blank name: %s", row)
                continue

            try:
                ward_number = int(row["ward_number"])
            except (TypeError, ValueError):
                raise SystemExit(f"Row {row}: ward_number is not an integer")

            district = (
                db.query(District)
                .filter(District.name == d_name, District.state_name == state_name)
                .first()
            )
            if district is None:
                district = District(
                    id=uuid.uuid4(),
                    name=d_name,
                    state_name=state_name,
                    centroid_lat=_float(row, "district_lat"),
                    centroid_lon=_float(row, "district_lon"),
                )
                db.add(district)
                db.flush()  # assign the PK for the children below
                created["district"] += 1
                logger.info("District created: %s", d_name)
            else:
                skipped["district"] += 1

            taluka = (
                db.query(Taluka)
                .filter(Taluka.name == t_name, Taluka.district_id == district.id)
                .first()
            )
            if taluka is None:
                taluka = Taluka(
                    id=uuid.uuid4(),
                    name=t_name,
                    district_id=district.id,
                    centroid_lat=_float(row, "taluka_lat"),
                    centroid_lon=_float(row, "taluka_lon"),
                )
                db.add(taluka)
                db.flush()
                created["taluka"] += 1
                logger.info("  Taluka created: %s / %s", d_name, t_name)
            else:
                skipped["taluka"] += 1

            # Match on ward_number within the taluka, not on name: the number is
            # the stable identifier and names get re-spelled.
            ward = (
                db.query(Ward)
                .filter(Ward.taluka_id == taluka.id, Ward.ward_number == ward_number)
                .first()
            )
            if ward is None:
                db.add(
                    Ward(
                        id=uuid.uuid4(),
                        name=w_name,
                        ward_number=ward_number,
                        taluka_id=taluka.id,
                        centroid_lat=_float(row, "ward_lat"),
                        centroid_lon=_float(row, "ward_lon"),
                    )
                )
                created["ward"] += 1
                logger.info("    Ward created: %s / %s / %s (#%s)", d_name, t_name, w_name, ward_number)
            else:
                skipped["ward"] += 1

        if dry_run:
            db.rollback()
            logger.info("DRY RUN — rolled back, nothing was written")
        else:
            db.commit()

        logger.info(
            "Created %d district(s), %d taluka(s), %d ward(s); "
            "existing rows left alone: %d/%d/%d",
            created["district"], created["taluka"], created["ward"],
            skipped["district"], skipped["taluka"], skipped["ward"],
        )
        return 0

    except Exception as e:
        db.rollback()
        logger.error("Seeding failed, rolled back: %s", e, exc_info=True)
        return 1
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--file", type=Path, default=DEFAULT_CSV, help="CSV to read")
    parser.add_argument(
        "--state",
        default=os.getenv("SEED_STATE_NAME", "Gujarat"),
        help="State name for the districts (default: Gujarat)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report changes without writing"
    )
    args = parser.parse_args()
    return seed(args.file, args.state, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
