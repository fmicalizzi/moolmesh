"""Offline tests for reference/ops_snapshot.py. Run: python3 -m unittest discover -s tests"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "reference"))
import ops_snapshot as ops  # noqa: E402

TZ = ZoneInfo("America/Costa_Rica")  # UTC-6, no DST


class LocalDay(unittest.TestCase):
    def test_early_utc_belongs_to_previous_local_day(self):
        # 03:30 UTC on Nov 3 is 21:30 local on Nov 2
        self.assertEqual(ops.local_day("2026-11-03T03:30:00Z", TZ), date(2026, 11, 2))

    def test_afternoon_utc_same_day(self):
        self.assertEqual(ops.local_day("2026-11-03T16:00:00Z", TZ), date(2026, 11, 3))


class Identity(unittest.TestCase):
    def test_cross_repo_reference_is_qualified(self):
        ref = {"number": 5, "repository": {"name": "backlog", "owner": {"login": "acme-dev"}}}
        self.assertEqual(ops.ref_key(ref, "acme-dev", "portal"), "acme-dev/backlog#5")

    def test_bots(self):
        for login in ("app/dependabot", "dependabot[bot]", "renovate"):
            self.assertTrue(ops.is_bot(login, {"renovate"}))
        self.assertFalse(ops.is_bot("alice", {"renovate"}))


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(os.path.join(ROOT, "examples", "data", "snapshots", "1001"),
                        os.path.join(self.tmp, "snapshots", "1001"))
        # keep only the seed snapshot
        for f in os.listdir(os.path.join(self.tmp, "snapshots", "1001")):
            if f != "2026-11-01.json":
                os.remove(os.path.join(self.tmp, "snapshots", "1001", f))
        self.src = ops.FixtureSource(os.path.join(ROOT, "examples", "fixtures"))
        self.org = {"id": "acme", "timezone": "America/Costa_Rica", "bots": []}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_portal(self, dry=False):
        repo = {"repo": "acme-dev/portal", "kind": "code", "team": ["alice", "bob"]}
        return ops.build(repo, self.org, {}, self.src, self.tmp, date(2026, 11, 3), None, dry)

    def test_chain_matches_live_and_bot_not_credited(self):
        s = self.run_portal()
        self.assertEqual((s["issues"]["total_closed"], s["issues"]["total_open"]), (5, 2))
        self.assertEqual(s["notes"], [])
        self.assertEqual(s["prs"]["merged_this_period"], 3)          # bot PR counts in totals
        self.assertNotIn("app/dependabot", s["by_developer"])         # ...but is never credited
        self.assertEqual(s["by_developer"]["bob"]["issues_closed"], 0)  # bob closed backlog#5, not portal#5

    def test_never_overwrites(self):
        self.run_portal()
        self.assertIsNone(self.run_portal())

    def test_chain_mismatch_is_reported(self):
        seed = os.path.join(self.tmp, "snapshots", "1001", "2026-11-01.json")
        d = json.load(open(seed))
        d["issues"]["total_open"] = 3  # previous cut was off by one
        json.dump(d, open(seed, "w"))
        s = self.run_portal()
        self.assertEqual(s["issues"]["total_open"], 2)               # live value wins
        self.assertTrue(s["notes"] and "adjusted to live" in s["notes"][0])



class Confidential(unittest.TestCase):
    def test_confidential_repo_must_not_be_tracked(self):
        reg = os.path.join(tempfile.mkdtemp(), "bad.toml")
        with open(reg, "w") as f:
            f.write('[organization]\nid="x"\ntimezone="UTC"\n'
                    '[[owners]]\nlogin="secret-acct"\ntype="user"\nrelation="confidential"\n'
                    '[[repos]]\nrepo="secret-acct/notes"\nkind="docs"\n')
        sys.argv = ["ops", "--registry", reg, "--data", tempfile.mkdtemp(), "--cut", "2026-11-03",
                    "--fixtures", os.path.join(ROOT, "examples", "fixtures")]
        self.assertEqual(ops.main(), 2)


if __name__ == "__main__":
    unittest.main()
