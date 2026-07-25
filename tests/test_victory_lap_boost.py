#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regression tests for the Proof of Play boost / Victory Lap interaction.

The Victory Lap is the top row of the Proof of Play boost table (README,
"Proof of Play Boost"), so it must not also be multiplied on top of the
session boost the daemon derives from that same state, and it must only be
paid once.

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
BOOST_CFG = CONFIG["proof_of_play"]["session_boost_multipliers"]


def _reload_modules(home: Path):
    """Import the bridge/daemon with STATE_DIR pointing at a temp HOME."""
    os.environ["HOME"] = str(home)
    for name in ("achievement_bridge", "proof_of_play"):
        sys.modules.pop(name, None)
    import achievement_bridge
    import proof_of_play
    return achievement_bridge, proof_of_play


class VictoryLapBoostTest(unittest.TestCase):
    def setUp(self):
        self._old_home = os.environ.get("HOME")
        self.home = Path(tempfile.mkdtemp(prefix="arcade-test-"))
        self.ab, self.pop = _reload_modules(self.home)
        self.sessions = self.home / ".rustchain-arcade" / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home

    def _write_session(self, duration_minutes, achievements, has_mastery):
        boost = self.pop.calculate_boost_multiplier(
            duration_seconds=duration_minutes * 60.0,
            achievements_earned=achievements,
            has_mastery=has_mastery,
            boost_config=BOOST_CFG,
        )
        (self.sessions / "current_session.json").write_text(json.dumps({
            "active": True,
            "session_id": "s1",
            "duration_minutes": duration_minutes,
            "achievements_earned": achievements,
            "boost_multiplier": boost,
        }))
        return boost

    def _activate_lap(self):
        self.ab.save_victory_lap({
            "active": True,
            "game_id": "1",
            "game_title": "Super Metroid",
            "activated_at": "2026-01-01T00:00:00+00:00",
            "epoch_used": False,
        })

    def test_lap_is_not_multiplied_with_the_session_boost_it_produced(self):
        """A pending lap makes the daemon report 5.0x; paying both gives 25x."""
        self._activate_lap()
        written = self._write_session(20.0, 3, has_mastery=True)
        self.assertEqual(written, BOOST_CFG["mastery_victory_lap"])

        lap = self.ab.get_victory_lap_multiplier()
        session_boost = self.ab.get_session_boost(CONFIG, include_victory_lap=False)

        self.assertEqual(session_boost, BOOST_CFG["15min"])
        self.assertEqual(max(session_boost, lap), BOOST_CFG["mastery_victory_lap"])
        self.assertNotEqual(written * lap, BOOST_CFG["mastery_victory_lap"])

    def test_playtime_tier_is_recovered_from_under_the_lap(self):
        self._activate_lap()
        for minutes, achievements, expected in (
            (5.0, 0, 1.0),
            (20.0, 0, BOOST_CFG["15min"]),
            (35.0, 1, BOOST_CFG["30min_with_achievement"]),
            (35.0, 0, BOOST_CFG["15min"]),
            (90.0, 0, BOOST_CFG["60min"]),
        ):
            with self.subTest(minutes=minutes, achievements=achievements):
                self._write_session(minutes, achievements, has_mastery=True)
                self.assertEqual(
                    self.ab.get_session_boost(CONFIG, include_victory_lap=False),
                    expected,
                )

    def test_session_boost_unchanged_when_no_lap_is_pending(self):
        written = self._write_session(90.0, 2, has_mastery=False)
        self.assertEqual(written, BOOST_CFG["60min"])
        self.assertEqual(self.ab.get_session_boost(CONFIG), written)
        self.assertEqual(self.ab.get_victory_lap_multiplier(), 1.0)

    def test_lap_still_pays_without_a_proof_of_play_session(self):
        self._activate_lap()
        session_boost = self.ab.get_session_boost(CONFIG, include_victory_lap=False)
        lap = self.ab.get_victory_lap_multiplier()
        self.assertEqual(session_boost, 1.0)
        self.assertEqual(max(session_boost, lap), BOOST_CFG["mastery_victory_lap"])

    def test_lap_is_consumed_after_one_use(self):
        self._activate_lap()
        self.assertIsNotNone(self.ab.consume_victory_lap())
        self.assertIsNone(self.ab.consume_victory_lap())
        self.assertEqual(self.ab.get_victory_lap_multiplier(), 1.0)


if __name__ == "__main__":
    unittest.main()
