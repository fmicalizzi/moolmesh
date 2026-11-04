"""The suite never touches the developer's real ~/.moolmesh (#44).

tests/conftest.py points HOME/USERPROFILE at a throwaway directory before the
first `hub` import; these tests pin that contract so a regression (an early
import, a new import-time path) fails loudly instead of migrating real stores.
"""

import os
from pathlib import Path

# The conftest module is never imported here (#49): under --import-mode=importlib
# that would re-execute it. The values come from the ``home_isolation`` fixture.


def test_home_points_at_throwaway_dir(home_isolation):
    conftest = home_isolation
    assert os.environ["HOME"] == conftest.TEST_HOME
    assert os.environ["USERPROFILE"] == conftest.TEST_HOME
    assert os.path.realpath(Path.home()) == os.path.realpath(conftest.TEST_HOME)
    assert os.path.realpath(os.path.expanduser("~")) == os.path.realpath(
        conftest.TEST_HOME
    )


def test_real_home_is_captured_and_distinct(home_isolation):
    conftest = home_isolation
    assert os.environ["MOOLMESH_TEST_REAL_HOME"] == conftest.REAL_HOME
    assert os.path.realpath(conftest.REAL_HOME) != os.path.realpath(
        conftest.TEST_HOME
    )


def test_every_import_time_path_is_inside_test_home(home_isolation):
    paths = home_isolation.home_derived_paths()
    assert len(paths) == 9
    assert home_isolation.outside_test_home() == {}


def test_default_stores_open_inside_test_home(home_isolation):
    conftest = home_isolation
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


def test_does_not_import_the_conftest_module():
    """#49: the file stays valid under --import-mode=importlib."""
    src = Path(__file__).read_text(encoding="utf-8")
    assert "import " + "conftest" not in src
    assert "from tests " + "import" not in src
