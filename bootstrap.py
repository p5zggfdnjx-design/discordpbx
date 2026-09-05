"""DiscordPBX runtime bootstrap.

Keeps compatibility-only feature layers isolated from the canonical bridge. The
Discord voice connection lifecycle itself is first-class source architecture in
``voice_lifecycle.py`` and is deliberately not patched at runtime.
"""

from auto_redial_guard import apply as apply_auto_redial_guard
from database_consolidation import apply as apply_database_consolidation
from discord_join_chime import apply as apply_discord_join_chime
from discord_sound_pack import apply as apply_discord_sound_pack
from github_release_guard import apply as apply_github_release_guard
from inbound_routing_guard import apply as apply_inbound_routing_guard
from inbound_stability_guard import apply as apply_inbound_stability_guard
from matrix_ui import apply as apply_matrix_ui
from reliability_guard import apply as apply_reliability_guard
from request_compat import install_cached_request_body_compat as apply_request_compat
from runtime_hotfix import apply as apply_runtime_hotfix


def apply_runtime_layers() -> None:
    # Voice connection ownership/prewarm/expiry/watchdog are native in
    # ReliableBridgeManager. Do not reintroduce inbound_voice_guard,
    # inbound_first_call_guard, or inbound_expiry_guard here: those older layers
    # create multiple reconnect owners and were the source of reconnect storms.
    apply_database_consolidation()
    apply_reliability_guard()
    apply_inbound_routing_guard()
    apply_discord_join_chime()
    apply_discord_sound_pack()
    apply_request_compat()
    apply_auto_redial_guard()
    apply_github_release_guard()
    apply_runtime_hotfix()
    apply_matrix_ui()
    apply_inbound_stability_guard()


apply_runtime_layers()

if __name__ == "__main__":
    import runpy

    runpy.run_module("bot", run_name="__main__")
