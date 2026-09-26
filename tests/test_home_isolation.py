"""The suite never touches the developer's real ~/.moolmesh (#44).

tests/conftest.py points HOME/USERPROFILE at a throwaway directory before the
first `hub` import; these tests pin that contract so a regression (an early
import, a new import-time path) fails loudly instead of migrating real stores.
"""

import os
from pathlib import Path

from tests import conftest


def test_home_points_at_throwaway_dir():
    assert os.environ["HOME"] == conftest.TEST_HOME
    assert os.environ["USERPROFILE"] == conftest.TEST_HOME
    assert os.path.realpath(Path.home()) == os.path.realpath(conftest.TEST_HOME)
    assert os.path.realpath(os.path.expanduser("~")) == os.path.realpath(
        conftest.TEST_HOME
    )


def test_real_home_is_captured_and_distinct():
    assert os.environ["MOOLMESH_TEST_REAL_HOME"] == conftest.REAL_HOME
    assert os.path.realpath(conftest.REAL_HOME) != os.path.realpath(
        conftest.TEST_HOME
    )


def test_every_import_time_path_is_inside_test_home():
    paths = conftest._home_derived_paths()
    assert len(paths) == 9
    assert conftest._outside_test_home() == {}


def test_default_stores_open_inside_test_home():
    from hub.cache.event_store import EventStore
    from hub.cache.workspace_store import WorkspaceStore

    root = os.path.realpath(conftest.TEST_HOME)
    ev = EventStore()
    ws = WorkspaceStore()
    try:
        for db in (ev.db_path, ws.db_path):
            assert os.path.realpath(db).startswith(root + os.sep)
    finally:
        ev.close()
        ws.close()
