from pathlib import Path
import runpy
import sys

from updater.migrate_state import run as migrate_runtime_state

migrate_runtime_state()

import webui
sys.modules.setdefault("webui_v3", webui)

from runtime_hotfix import apply as apply_updater_hotfix
from github_release_guard import apply as apply_github_release_guard
from matrix_ui import apply as apply_matrix_ui
from prefix_blocks import apply as apply_prefix_blocks
from contact_recovery import apply as apply_contact_recovery
from reliability_guard import apply as apply_reliability_guard
from global_contacts import apply as apply_global_contacts
from database_consolidation import apply as apply_database_consolidation
from history_mirror import apply as apply_history_mirror
from auto_redial_guard import apply as apply_auto_redial_guard
from inbound_voice_guard import apply as apply_inbound_voice_guard
from inbound_first_call_guard import apply as apply_inbound_first_call_guard
from discord_sound_pack import apply as apply_discord_sound_pack
from inbound_expiry_guard import apply as apply_inbound_expiry_guard
from inbound_routing_guard import apply as apply_inbound_routing_guard

apply_updater_hotfix()
apply_github_release_guard()
apply_matrix_ui()
apply_prefix_blocks()
apply_contact_recovery()
apply_reliability_guard()
apply_global_contacts()
apply_database_consolidation()
apply_history_mirror()
apply_auto_redial_guard()
apply_inbound_voice_guard()
apply_inbound_first_call_guard()
apply_discord_sound_pack()
apply_inbound_expiry_guard()
apply_inbound_routing_guard()
runpy.run_path(str(Path(__file__).with_name("bot.py")), run_name="__main__")
