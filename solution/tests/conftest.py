from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

# Deterministic test-only Fernet key. Production must provide its own stable secret.
os.environ.setdefault("PII_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")

SOLUTION_ROOT = Path(__file__).resolve().parents[1]
if str(SOLUTION_ROOT) not in sys.path:
    sys.path.insert(0, str(SOLUTION_ROOT))


@pytest.fixture
def database():
    """Create an isolated PostgreSQL schema for each integration test."""
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration test: set TEST_DATABASE_URL to a disposable database")

    from app.database import Database

    schema = f"test_{uuid.uuid4().hex}"
    db = Database(dsn, schema=schema)
    try:
        yield db
    finally:
        db.drop_schema()
