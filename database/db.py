import pandas as pd
from sqlalchemy import create_engine, text
from config.settings import DB_URL
from utils.logger import logger

engine = create_engine(DB_URL, pool_size=5, max_overflow=10, pool_pre_ping=True)


def get_engine():
    return engine


def get_connection():
    return engine.connect()


def execute_sql(sql: str, params: dict = None):
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        conn.commit()
        return result


def read_sql(query: str, params: dict = None) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn, params=params)


def write_df(df: pd.DataFrame, table: str, if_exists: str = "append"):
    df.to_sql(table, engine, if_exists=if_exists, index=False, method="multi")


_TABLES: dict = {}


def upsert_candles(df: pd.DataFrame, table: str = "minute_candles"):
    """Insert candles, ignoring rows that already exist (by timestamp+symbol)."""
    if df.empty:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy import MetaData
    tbl = _TABLES.get(table)
    if tbl is None:                       # reflect once per process, not on every call
        meta = MetaData()
        meta.reflect(bind=engine, only=[table])
        tbl = _TABLES[table] = meta.tables[table]
    rows = df.to_dict(orient="records")
    inserted = 0
    with engine.begin() as conn:
        for chunk_start in range(0, len(rows), 500):
            chunk = rows[chunk_start:chunk_start + 500]
            stmt = pg_insert(tbl).values(chunk).on_conflict_do_nothing(
                index_elements=["timestamp", "symbol"]
            )
            result = conn.execute(stmt)
            inserted += result.rowcount
    return inserted


def _sql_statements(sql: str) -> list[str]:
    """Split a schema file into executable statements (drops comment-only chunks)."""
    out = []
    for chunk in sql.split(";"):
        body = "\n".join(l for l in chunk.splitlines() if not l.strip().startswith("--")).strip()
        if body:
            out.append(body)
    return out


def init_db():
    """
    Run schema.sql to create all tables (and hypertables when TimescaleDB is installed).

    Every statement runs in its OWN transaction: one failing statement (e.g. the TimescaleDB
    extension not being installed) must not abort the rest, otherwise no tables get created.
    Without TimescaleDB the tables are plain PostgreSQL tables and everything still works.
    """
    import os
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r") as f:
        statements = _sql_statements(f.read())

    timescale_missing = False
    failed = 0
    for stmt in statements:
        is_ts = "timescaledb" in stmt.lower() or "create_hypertable" in stmt.lower()
        if is_ts and timescale_missing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            if is_ts:
                timescale_missing = True
                logger.warning("TimescaleDB not available — using plain PostgreSQL tables "
                               "(install timescaledb for hypertable performance).")
            else:
                failed += 1
                logger.error(f"Schema statement failed: {str(e).splitlines()[0]} | {stmt[:80]!r}")
    if failed:
        logger.error(f"Database schema initialised with {failed} error(s).")
    else:
        logger.info("Database schema initialized.")
