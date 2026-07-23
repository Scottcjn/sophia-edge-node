#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regression tests for the achievement-velocity anti-cheat.

anti_cheat.max_achievements_per_hour has to hold inside a single poll as
well: get_recent_achievements() returns a 60-minute window, so one cycle can
carry an hour's worth of unlocks.

Run with:  python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CONFIG = json.loads((REPO_ROOT / "config.json").read_text())
MAX_PER_HOUR = CONFIG["anti_cheat"]["max_achievements_per_hour"]


def _make_achievements(count, points=5, hardcore=1, offset=0):
    return [
        {"AchievementID": 1000 + offset + i, "Title": f"Ach {offset + i}",
         "Points": points, "HardcoreMode": hardcore,
         "GameID": 1, "GameTitle": "Super Metroid"}
        for i in range(count)
    ]


class _FakeClient:
    username = "player"

    def __init__(self, achievements):
        self.achievements = achievements

    def get_user_profile(self, username=None):
        return {"ULID": "ULID-PERMANENT", "User": "player"}

    def get_recent_achievements(self, minutes=60):
        return self.achievements

    def get_achievement_unlock_rate(self, game_id):
        return {str(a["AchievementID"]): 80.0 for a in self.achievements}

    def get_game_progress(self, game_id):
        return {"NumAchievements": 500, "ConsoleName": "SNES",
                "NumAwardedToUser": len(self.achievements),
                "NumAwardedToUserHardcore": len(self.achievements)}


class VelocityLimitTest(unittest.TestCase):
    def setUp(self):
        self._old_home = os.environ.get("HOME")
        self.home = Path(tempfile.mkdtemp(prefix="arcade-test-"))
        os.environ["HOME"] = str(self.home)
        os.environ.setdefault("RA_USERNAME", "player")
        os.environ.setdefault("RA_API_KEY", "k")
        sys.modules.pop("achievement_bridge", None)
        import achievement_bridge
        self.ab = achievement_bridge
        self.paid = []
        self.ab.submit_achievement_reward = (
            lambda config, ach, tier, rtc, hc, **kw:
            self.paid.append((ach["AchievementID"], rtc)) or True
        )
        self.ab.submit_mastery_bonus = lambda *a, **kw: True
        self.ab.submit_pending_rewards = lambda config: 0

    def tearDown(self):
        sys.modules.pop("achievement_bridge", None)
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home

    def _run(self, achievements):
        client = _FakeClient(achievements)
        self.ab.RetroAchievementsClient = lambda *a, **kw: client
        self.ab.process_achievements(CONFIG)

    def test_a_single_poll_cannot_exceed_the_hourly_limit(self):
        self._run(_make_achievements(MAX_PER_HOUR * 2))
        self.assertEqual(len(self.paid), MAX_PER_HOUR)

    def test_unpaid_unlocks_are_not_marked_reported(self):
        achievements = _make_achievements(MAX_PER_HOUR * 2)
        self._run(achievements)
        reported = set(self.ab.load_reported().get("achievements", []))
        over_limit = {str(a["AchievementID"]) for a in achievements[MAX_PER_HOUR:]}
        self.assertFalse(reported & over_limit)

    def test_batch_under_the_limit_is_paid_in_full(self):
        count = MAX_PER_HOUR - 1
        self._run(_make_achievements(count))
        self.assertEqual(len(self.paid), count)
        self.assertEqual(
            len(self.ab.load_velocity_tracker().get("timestamps", [])), count)

    def test_limit_accounts_for_unlocks_already_recorded_this_hour(self):
        for _ in range(MAX_PER_HOUR - 3):
            self.ab.record_achievement_timestamp()
        self._run(_make_achievements(10))
        self.assertEqual(len(self.paid), 3)

    def test_skipped_unlocks_do_not_consume_the_hourly_budget(self):
        # hardcore_only is on by default, so softcore unlocks are never paid
        # and must not count towards the limit either.
        self._run(_make_achievements(5, hardcore=0) + _make_achievements(5, offset=50))
        self.assertEqual(
            len(self.ab.load_velocity_tracker().get("timestamps", [])), 5)


if __name__ == "__main__":
    unittest.main()
