import sys
from types import SimpleNamespace

sys.modules.setdefault(
    "requests",
    SimpleNamespace(Session=object, RequestException=Exception),
)

from game_recommender import (
    estimate_rtc_potential,
    estimate_time_hours,
    filter_hidden_gems,
    filter_near_mastery,
    rtc_per_hour,
)


def reward_config():
    return {
        "achievements": {
            "hardcore_multiplier": 2.0,
            "reward_tiers": {
                "common": {"rtc": 0.00005},
                "uncommon": {"rtc": 0.0002},
                "rare": {"rtc": 0.0005},
                "ultra_rare": {"rtc": 0.001},
                "legendary": {"rtc": 0.005},
            },
        },
        "rarity_factors": {
            "common_above_50pct": 1.0,
            "uncommon_20_50pct": 1.25,
            "rare_5_20pct": 1.75,
            "ultra_rare_1_5pct": 2.5,
            "legendary_below_1pct": 3.0,
        },
        "mastery_milestones": {
            "full_mastery": 0.02,
            "legendary_mastery": 0.05,
        },
    }


def test_estimate_rtc_potential_uses_remaining_achievements_and_started_bonus():
    game = {
        "NumAchievements": 20,
        "NumAwardedToUser": 5,
        "NumDistinctPlayers": 150,
        "points_total": 200,
    }

    assert estimate_rtc_potential(game, reward_config()) == 0.1075


def test_estimate_rtc_potential_returns_zero_for_completed_or_empty_games():
    config = reward_config()

    assert estimate_rtc_potential(
        {"NumAchievements": 10, "NumAwardedToUser": 10, "points_total": 100},
        config,
    ) == 0.0
    assert estimate_rtc_potential(
        {"NumAchievements": 0, "NumAwardedToUser": 0, "points_total": 0},
        config,
    ) == 0.0


def test_time_and_rate_helpers_handle_normal_and_zero_cases():
    assert estimate_time_hours({"NumAchievements": 30, "NumAwardedToUser": 0}) == 1.5
    assert estimate_time_hours({"NumAchievements": 10, "NumAwardedToUser": 10}) == 0.0
    assert rtc_per_hour(0.12, 2.0) == 0.06
    assert rtc_per_hour(0.12, 0.0) == 0.0


def test_filter_near_mastery_keeps_started_incomplete_games_at_threshold():
    games = [
        {"Title": "Nearly Done", "NumAchievements": 10, "NumAwardedToUser": 8},
        {"Title": "Finished", "NumAchievements": 10, "NumAwardedToUser": 10},
        {"Title": "Not Started", "NumAchievements": 10, "NumAwardedToUser": 0},
        {"Title": "Below Threshold", "NumAchievements": 10, "NumAwardedToUser": 7},
    ]

    filtered = filter_near_mastery(games, threshold=80.0)

    assert [game["Title"] for game in filtered] == ["Nearly Done"]
    assert filtered[0]["completion_pct"] == 80.0
    assert filtered[0]["remaining_achievements"] == 2


def test_filter_hidden_gems_requires_low_player_count_and_real_achievement_set():
    games = [
        {"Title": "Tiny Community", "NumDistinctPlayers": 200, "NumAchievements": 10},
        {"Title": "Casual Fallback", "NumDistinctPlayersCasual": 12, "NumAchievements": 12},
        {"Title": "Too Popular", "NumDistinctPlayers": 201, "NumAchievements": 10},
        {"Title": "Too Few Achievements", "NumDistinctPlayers": 50, "NumAchievements": 9},
        {"Title": "No Players", "NumDistinctPlayers": 0, "NumAchievements": 10},
    ]

    filtered = filter_hidden_gems(games, max_players=200)

    assert [game["Title"] for game in filtered] == ["Tiny Community", "Casual Fallback"]
    assert filtered[0]["player_count"] == 200
    assert filtered[1]["player_count"] == 12
