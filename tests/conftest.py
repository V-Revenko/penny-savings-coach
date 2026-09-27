import shutil

import pytest

from app import data


@pytest.fixture(scope="session", autouse=True)
def isolated_database(tmp_path_factory):
    """Run every test against a temporary copy of data/gesa.db.

    Tests write and clear coach_checkin rows. Without this they would share the
    live database with a running demo server, wiping its check-in history and
    losing their own rows whenever someone clicks in the browser.
    """
    live = data.DB_PATH
    if not live.exists():
        yield
        return
    copy = tmp_path_factory.mktemp("db") / "gesa.db"
    shutil.copy(live, copy)
    mp = pytest.MonkeyPatch()
    mp.setattr(data, "DB_PATH", copy)
    data.load_tables.cache_clear()
    yield
    mp.undo()
    data.load_tables.cache_clear()


@pytest.fixture(scope="session")
def raw():
    return data.read_workbook()


@pytest.fixture(scope="session")
def tables(raw):
    return data.to_canonical(raw)
