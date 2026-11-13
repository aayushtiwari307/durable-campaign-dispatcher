from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    """PostgreSQL connection pool and versioned SQL migration runner.

    The application uses a synchronous psycopg pool for short SQL transactions.
    Callers that run inside the asyncio event loop dispatch repository operations
    through asyncio.to_thread so database I/O never blocks that loop.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 10.0,
        migration_dir: str | Path | None = None,
        schema: str | None = None,
    ) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
        if schema is not None and not schema.replace("_", "").isalnum():
            raise ValueError("PostgreSQL schema must contain only letters, digits and underscores")
        self.dsn = dsn
        self.schema = schema
        self._dispatcher_lock_connection: Connection | None = None
        self.migration_dir = (
            Path(migration_dir).resolve()
            if migration_dir is not None
            else Path(__file__).resolve().parents[1] / "migrations"
        )
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            kwargs={"row_factory": dict_row},
            open=True,
            name="campaign-dispatcher",
        )
        try:
            self.pool.wait(timeout=timeout)
            if self.schema is not None:
                with self.pool.connection() as connection:
                    connection.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
                    )
        except Exception:
            self.pool.close()
            raise

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        """Borrow a connection with the configured isolated schema, if any."""
        with self.pool.connection() as connection:
            if self.schema is not None:
                connection.execute(
                    sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
                )
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.connect() as connection:
            with connection.transaction():
                yield connection

    def initialize(self) -> None:
        if not self.migration_dir.is_dir():
            raise FileNotFoundError(f"SQL migration directory not found: {self.migration_dir}")
        migration_files = sorted(self.migration_dir.glob("*.sql"))
        if not migration_files:
            raise RuntimeError(f"No SQL migrations found in {self.migration_dir}")
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

        for migration_path in migration_files:
            version = migration_path.name
            with self.connect() as connection:
                applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s",
                    (version,),
                ).fetchone()
            if applied:
                continue

            statements = [
                statement.strip()
                for statement in migration_path.read_text(encoding="utf-8").split(";")
                if statement.strip()
            ]
            with self.transaction() as connection:
                # Serialize migration runners; a second process must not apply
                # the same schema change concurrently.
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('campaign_dispatcher_schema'))"
                )
                applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s FOR UPDATE",
                    (version,),
                ).fetchone()
                if applied:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (%s)",
                    (version,),
                )

    def health_check(self) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 AS healthy").fetchone()["healthy"] == 1

    def try_acquire_dispatcher_lock(self) -> bool:
        """Allow one scheduler per database schema; webhook handling remains multi-worker safe.

        The assignment supports a single dispatcher process. A PostgreSQL session-level
        advisory lock prevents an accidental second Uvicorn worker/replica from running
        another dispatch loop against the same provider world. The dedicated connection
        stays open for the scheduler lifetime, so process death releases the lock.
        """
        if self._dispatcher_lock_connection is not None:
            return True
        connection = psycopg.connect(
            self.dsn, autocommit=True, row_factory=dict_row
        )
        try:
            row = connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s)) AS acquired",
                ("campaign_dispatcher_scheduler", self.schema or "public"),
            ).fetchone()
            if row["acquired"]:
                self._dispatcher_lock_connection = connection
                return True
        except Exception:
            connection.close()
            raise
        connection.close()
        return False

    def release_dispatcher_lock(self) -> None:
        connection = self._dispatcher_lock_connection
        self._dispatcher_lock_connection = None
        if connection is not None:
            try:
                connection.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                    ("campaign_dispatcher_scheduler", self.schema or "public"),
                )
            finally:
                connection.close()

    def drop_schema(self) -> None:
        """Drop an isolated test schema after closing this pool."""
        if self.schema is None:
            raise RuntimeError("Refusing to drop the default schema")
        schema = self.schema
        try:
            self.release_dispatcher_lock()
        finally:
            self.pool.close()
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )

    def close(self) -> None:
        try:
            self.release_dispatcher_lock()
        finally:
            self.pool.close()
