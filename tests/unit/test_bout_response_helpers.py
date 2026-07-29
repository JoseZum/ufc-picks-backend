import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/ufc_picks_test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-at-least-32-characters")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")

from app.controllers.bouts_controller import (
    _is_ufc_profile_url,
    _normalize_weight_class,
    _process_fighters,
)


def test_normalize_weight_class_removes_duplicate_bout_phrases():
    assert (
        _normalize_weight_class("Bantamweight Bout Bantamweight Bout")
        == "Bantamweight"
    )
    assert _normalize_weight_class("Heavyweight Bout Heavyweight Bout") == "Heavyweight"
    assert _normalize_weight_class("Light Heavyweight Bout") == "Light Heavyweight"


def test_espn_headshot_replaces_old_ufc_fight_card_asset():
    espn_url = "https://a.espncdn.com/i/headshots/mma/players/full/1.png"
    fighters = {
        "red": {
            "fighter_name": "Test Fighter",
            "profile_image_url": "https://ufc.com/images/old-torso.png",
            "espn_headshot_url": espn_url,
        }
    }

    assert _process_fighters(fighters)["red"]["profile_image_url"] == espn_url


def test_ufc_fight_card_asset_is_not_used_without_espn_photo():
    ufc_url = "https://www.ufcespanol.com/images/old-torso.png"
    assert _is_ufc_profile_url(ufc_url)
    fighters = {
        "red": {
            "fighter_name": "Test Fighter",
            "profile_image_url": ufc_url,
        }
    }

    assert _process_fighters(fighters)["red"]["profile_image_url"] is None
