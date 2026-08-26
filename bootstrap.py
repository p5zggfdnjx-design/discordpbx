from pathlib import Path
import runpy
import sys

# runtime_hotfix was originally written against an internal module name that is
# not present in packaged releases. Alias the real webui module before applying
# the hotfix so startup cannot fail with ModuleNotFoundError.
import webui
sys.modules.setdefault("webui_v3", webui)

from runtime_hotfix import apply

apply()
runpy.run_path(str(Path(__file__).with_name("bot.py")), run_name="__main__")
