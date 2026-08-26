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

apply_updater_hotfix()
apply_matrix_ui()
apply_prefix_blocks()
runpy.run_path(str(Path(__file__).with_name("bot.py")), run_name="__main__")
