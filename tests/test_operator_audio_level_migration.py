import json

from operator_settings import OperatorSettingsStore


def test_new_install_uses_louder_discord_to_caller_default(tmp_path):
    path = tmp_path / "operator_settings.json"
    store = OperatorSettingsStore(str(path))

    settings = store.get()

    assert settings["discord_to_caller_gain"] == 1.35
    assert settings["audio_level_schema"] == 1


def test_old_untouched_default_is_migrated_once(tmp_path):
    path = tmp_path / "operator_settings.json"
    path.write_text(
        json.dumps(
            {
                "ringback_muted": False,
                "caller_to_discord_gain": 1.0,
                "discord_to_caller_gain": 1.0,
                "inbound_chime_gain": 1.0,
                "voicemail_detection_enabled": True,
            }
        ),
        encoding="utf-8",
    )

    store = OperatorSettingsStore(str(path))
    settings = store.get()

    assert settings["discord_to_caller_gain"] == 1.35
    assert settings["audio_level_schema"] == 1
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["discord_to_caller_gain"] == 1.35
    assert persisted["audio_level_schema"] == 1


def test_explicit_custom_gain_is_not_overwritten(tmp_path):
    path = tmp_path / "operator_settings.json"
    path.write_text(
        json.dumps(
            {
                "discord_to_caller_gain": 1.7,
                "audio_level_schema": 0,
            }
        ),
        encoding="utf-8",
    )

    store = OperatorSettingsStore(str(path))
    settings = store.get()

    assert settings["discord_to_caller_gain"] == 1.7
    assert settings["audio_level_schema"] == 1


def test_user_can_still_choose_one_x_after_migration(tmp_path):
    path = tmp_path / "operator_settings.json"
    store = OperatorSettingsStore(str(path))
    store.set_audio_gains(
        caller_to_discord=1.0,
        discord_to_caller=1.0,
        inbound_chime=1.0,
    )

    reloaded = OperatorSettingsStore(str(path)).get()

    assert reloaded["discord_to_caller_gain"] == 1.0
    assert reloaded["audio_level_schema"] == 1
