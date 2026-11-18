import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="hoptrace_test_")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault(
    "FIXTURES_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "fixtures"),
)

from fastapi.testclient import TestClient  # noqa: E402

from app.db import DB_PATH, connect  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import init_db  # noqa: E402


@pytest.fixture()
def fresh_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
    yield
    connect().close()


@pytest.fixture()
def client(fresh_db):
    with TestClient(app) as c:
        yield c
