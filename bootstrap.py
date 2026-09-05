"""DiscordPBX runtime bootstrap.

Preserves the established v3 migration/feature-layer order while keeping Discord
voice connection ownership native in ``voice_lifecycle.py``. The legacy voice
reconnect/prewarm/expiry monkeypatches are intentionally not applied here.
"""

from pathlib import Path
import runpy
import sys

from updater.migrate_state import run as migrate_runtime_state

# State migration must happen before the web/runtime layers touch persisted data.
migrate_runtime_state()

# ``webui.py`` is the canonical v3 operator server. Keep the historical module
# alias for older compatibility layers that still import ``webui_v3``.
import webui
sys.modules.setdefault("webui_v3", webui)

from audio_meter import apply as apply_audio_meter
from auto_redial_guard import apply as apply_auto_redial_guard
from contact_recovery import apply as apply_contact_recovery
from database_consolidation import apply as apply_database_consolidation
from discord_join_chime import apply as apply_discord_join_chime
from discord_sound_pack import apply as apply_discord_sound_pack
from github_release_guard import apply as apply_github_release_guard
from global_contacts import apply as apply_global_contacts
from history_mirror import apply as apply_history_mirror
from inbound_routing_guard import apply as apply_inbound_routing_guard
from inbound_stability_guard import apply as apply_inbound_stability_guard
from matrix_ui import apply as apply_matrix_ui
from prefix_blocks import apply as apply_prefix_blocks
from reliability_guard import apply as apply_reliability_guard
from request_compat import install_cached_request_body_compat as apply_request_compat
from runtime_hotfix import apply as apply_updater_hotfix


def apply_runtime_layers() -> None:
    # Keep the proven non-voice runtime stack intact. In particular, updater,
    # contact, history, number-block, database, and meter layers must not vanish
    # merely because the voice lifecycle moved from monkeypatches into source.
    apply_updater_hotfix()
    apply_github_release_guard()
    apply_matrix_ui()
    apply_prefix_blocks()
    apply_contact_recovery()
    apply_reliability_guard()
    apply_global_contacts()
    apply_database_consolidation()
    apply_history_mirror()
    apply_request_compat()
    apply_auto_redial_guard()
    apply_discord_join_chime()
    apply_discord_sound_pack()
    apply_inbound_routing_guard()
    apply_audio_meter()

    # Do NOT apply inbound_voice_guard, inbound_first_call_guard, or
    # inbound_expiry_guard. ReliableBridgeManager owns connection, reconnect,
    # prewarm, pending TTL, watchdog, and idle departure as one state machine.

    # Must remain last: synchronizes the fully wrapped inbound path and limits
    # hangup-cue bursts without bypassing any earlier call/routing behavior.
    apply_inbound_stability_guard()


apply_runtime_layers()

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("bot.py")), run_name="__main__")
