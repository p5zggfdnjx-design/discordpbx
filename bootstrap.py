from pathlib import Path
import runpy
import sys

from updater.migrate_state import run as migrate_runtime_state

migrate_runtime_state()

import webui
sys.modules.setdefault("webui_v3", webui)

from runtime_hotfix import apply as apply_updater_hotfix
from matrix_ui import apply as apply_matrix_ui
from prefix_blocks import apply as apply_prefix_blocks
from contact_recovery import apply as apply_contact_recovery
from reliability_guard import apply as apply_reliability_guard
from global_contacts import apply as apply_global_contacts
from database_consolidation import apply as apply_database_consolidation
from history_mirror import apply as apply_history_mirror
from auto_redial_guard import apply as apply_auto_redial_guard

apply_updater_hotfix()
apply_matrix_ui()
apply_prefix_blocks()
apply_contact_recovery()
apply_reliability_guard()
apply_global_contacts()
apply_database_consolidation()
apply_history_mirror()
apply_auto_redial_guard()
runpy.run_path(str(Path(__file__).with_name("bot.py")), run_name="__main__")
