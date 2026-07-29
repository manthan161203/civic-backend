"""
Shared pytest fixtures.
=======================

Every test runs against a **real Postgres**, not SQLite. The application uses
``UUID(as_uuid=True)``, ``JSON``, native ``Enum`` types, partial indexes and
``pg_try_advisory_lock`` — a SQLite substitute would either fail to create the
schema or, worse, quietly diverge from production behaviour on exactly the
timezone and constraint semantics these tests exist to pin down.

Isolation is by transaction rollback, not by re-creating the schema: each test
gets a connection with an open transaction, the session is bound to it, and the
transaction is rolled back at teardown. Tests are therefore independent and the
suite does not get slower as it grows.

Run with::

    docker compose --profile test run --rm tests

or, against a database you already have::

    TEST_DATABASE_URL=postgresql://civic:civic@localhost:5434/civic_test pytest
"""

import itertools
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

# Settings must be import-safe before anything pulls in app.core.config.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-use-0123456789abcdef")
os.environ.setdefault("SMS_BACKEND", "console")
os.environ.setdefault("EMAIL_BACKEND", "console")
os.environ.setdefault("PUSH_BACKEND", "console")
os.environ.setdefault("AADHAAR_BACKEND", "console")
os.environ.setdefault("GOOGLE_AUTH_BACKEND", "console")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")
os.environ.setdefault("LOG_TO_FILE", "false")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql://civic:civic@db:5432/civic"),
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from app.core.security import create_access_token  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models.location import District, Taluka, Ward  # noqa: E402
from app.models.user import User  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    """Session-wide engine, with the schema rebuilt from the models.

    ``create_all`` is a no-op on a table that already exists — it never issues
    ``ALTER``. Because the test database lives in a persistent volume, that made
    a stale schema invisible: adding a column to a model left the old table in
    place, and the suite reported green while testing against a shape the code
    no longer had. The failure that surfaces is a raw ``UndefinedColumn`` from
    psycopg2 in whichever test happens to insert first, which points nowhere
    near the actual cause.

    Dropping first makes the schema a pure function of the models, which is the
    property the rest of this file already assumes.

    Migrations are still verified separately (``alembic upgrade head`` +
    ``alembic check``) — nothing here exercises them, so a correct model with a
    missing migration passes this suite and fails a deploy.
    """
    # Refuse to do this to anything that is not obviously a scratch database.
    # TEST_DATABASE_URL comes from the environment, and a drop_all against a
    # developer's real database would be unrecoverable.
    db_name = make_url(TEST_DATABASE_URL).database or ""
    if "test" not in db_name.lower():
        raise RuntimeError(
            f"Refusing to rebuild the schema in database {db_name!r}: the suite "
            "drops every table, so TEST_DATABASE_URL must name a test database."
        )

    eng = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    # Import every model so Base.metadata is complete before create_all.
    import app.models  # noqa: F401

    Base.metadata.drop_all(bind=eng)
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """A session wrapped in a transaction that is rolled back after the test.

    Nothing a test writes survives it, so tests can run in any order and the
    developer database is never polluted.

    Most tests reach the database through route handlers, which call
    ``db.commit()``. That commit must not end the outer transaction, so the
    session runs inside a SAVEPOINT and the ``after_transaction_end`` listener
    opens a fresh one each time the previous is released. This is SQLAlchemy's
    documented "join an external transaction" recipe; without the listener only
    the first commit in a test would be isolated.
    """
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    session = Session()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(db):
    """A TestClient whose ``get_db`` dependency yields the rollback session."""
    from app.main import app

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Location hierarchy ────────────────────────────────────────────────────────


@pytest.fixture
def hierarchy(db):
    """Two wards in two different talukas of one district.

    Two of everything, because most of the authorization bugs this suite covers
    are only visible when you ask whether admin A can reach ward B.
    """
    district = District(id=uuid.uuid4(), name="Test District", state_name="Gujarat")
    other_district = District(id=uuid.uuid4(), name="Other District", state_name="Gujarat")
    db.add_all([district, other_district])
    db.flush()

    taluka = Taluka(id=uuid.uuid4(), name="Test Taluka", district_id=district.id)
    other_taluka = Taluka(id=uuid.uuid4(), name="Other Taluka", district_id=other_district.id)
    db.add_all([taluka, other_taluka])
    db.flush()

    ward = Ward(id=uuid.uuid4(), name="Test Ward", ward_number=1, taluka_id=taluka.id)
    other_ward = Ward(id=uuid.uuid4(), name="Other Ward", ward_number=2, taluka_id=other_taluka.id)
    db.add_all([ward, other_ward])
    db.flush()

    return {
        "district": district,
        "other_district": other_district,
        "taluka": taluka,
        "other_taluka": other_taluka,
        "ward": ward,
        "other_ward": other_ward,
    }


# ── Users and tokens ──────────────────────────────────────────────────────────


_phone_counter = itertools.count(10_000_000)


def make_user(db, role="citizen", **kwargs) -> User:
    """Create and flush a user with a unique phone.

    The phone must be **numeric**: POST /auth/login decides whether an identifier
    is a phone or an email by whether it parses as digits, so a hex-derived
    value gets looked up in the email column and silently 401s.
    """
    serial = next(_phone_counter)
    defaults = dict(
        id=uuid.uuid4(),
        phone=f"+91{serial}",
        name=f"{role}-{serial}",
        role=role,
        is_active=True,
        language="en",
    )
    defaults.update(kwargs)
    user = User(**defaults)
    db.add(user)
    db.flush()
    return user


def auth_header(user: User) -> dict:
    """Bearer header carrying a valid access token for ``user``."""
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


@pytest.fixture
def citizen(db):
    return make_user(db, "citizen")


@pytest.fixture
def other_citizen(db):
    return make_user(db, "citizen")


@pytest.fixture
def worker(db):
    return make_user(db, "worker")


@pytest.fixture
def other_worker(db):
    return make_user(db, "worker")


@pytest.fixture
def ward_admin(db, hierarchy):
    return make_user(db, "ward_admin", ward_id=hierarchy["ward"].id)


@pytest.fixture
def other_ward_admin(db, hierarchy):
    return make_user(db, "ward_admin", ward_id=hierarchy["other_ward"].id)


@pytest.fixture
def taluka_admin(db, hierarchy):
    return make_user(db, "taluka_admin", taluka_id=hierarchy["taluka"].id)


@pytest.fixture
def super_admin(db):
    return make_user(db, "admin")
