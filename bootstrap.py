from pathlib import Path
import runpy

from runtime_hotfix import apply

apply()
runpy.run_path(str(Path(__file__).with_name("bot.py")), run_name="__main__")
