"""C4: Alembic owns the schema. init_db upgrades empty/versioned databases and stamps pre-Alembic ones."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from aecom import db


def _head() -> str:
    return ScriptDirectory.from_config(db._alembic_config()).get_current_head()


def _version() -> str | None:
    with db.get_engine().connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def test_baseline_is_the_single_head():
    assert _head() == "0001_baseline"


def test_empty_database_is_upgraded_to_head(tmp_path):
    db.reset_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    db.init_db()
    tables = set(inspect(db.get_engine()).get_table_names())
    assert set(db.Base.metadata.tables) | {"alembic_version"} == tables
    assert _version() == _head()


def test_models_and_migrations_do_not_drift(tmp_path):
    """If this fails, a model changed without a revision: `uv run alembic revision --autogenerate -m ...`."""
    db.reset_engine(f"sqlite:///{tmp_path / 'drift.db'}")
    db.init_db()
    with db.get_engine().connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "render_as_batch": True})
        assert compare_metadata(ctx, db.Base.metadata) == []


def test_legacy_database_gets_columns_backfill_and_stamp(tmp_path):
    db.reset_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with db.get_engine().begin() as conn:  # pre-Alembic install: old ops_events/commerce_orders, no alembic_version
        conn.execute(text("CREATE TABLE ops_events (id INTEGER PRIMARY KEY, ts DATETIME, topic VARCHAR, "
                          "payload JSON, source VARCHAR, processed BOOLEAN)"))
        conn.execute(text("INSERT INTO ops_events (topic, payload, source, processed) VALUES ('x', '{}', 's', 0)"))
        conn.execute(text("CREATE TABLE commerce_orders (id VARCHAR PRIMARY KEY, customer_id VARCHAR, product_id VARCHAR, "
                          "quantity INTEGER, amount_eur FLOAT, vat_eur FLOAT, campaign_id VARCHAR, status VARCHAR, created_at DATETIME)"))
        conn.execute(text("INSERT INTO commerce_orders (id, status, amount_eur) VALUES ('o1', 'delivered', 1), ('o2', 'paid', 1)"))
    db.init_db()
    insp = inspect(db.get_engine())
    assert {"attempts", "next_attempt_at", "done_handlers", "last_error", "dead"} <= {
        c["name"] for c in insp.get_columns("ops_events")}
    assert set(db.Base.metadata.tables) <= set(insp.get_table_names())  # missing tables created too
    with db.get_engine().begin() as conn:
        assert dict(conn.execute(text("SELECT id, email_sent FROM commerce_orders")).all()) == {"o1": 1, "o2": 0}
        assert conn.execute(text("SELECT topic, attempts, dead FROM ops_events")).one() == ("x", 0, 0)
    assert _version() == _head()


def test_second_init_on_versioned_database_is_idempotent(tmp_path):
    db.reset_engine(f"sqlite:///{tmp_path / 'versioned.db'}")
    db.init_db()
    with db.get_engine().begin() as conn:
        conn.execute(text("INSERT INTO ops_settings (key, value) VALUES ('k', 'v')"))
    db.init_db()
    with db.get_engine().begin() as conn:
        assert conn.execute(text("SELECT value FROM ops_settings WHERE key = 'k'")).scalar_one() == "v"
        assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1
    assert _version() == _head()
