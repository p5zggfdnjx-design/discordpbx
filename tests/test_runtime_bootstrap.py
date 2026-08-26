import subprocess
import sys
import unittest


class RuntimeBootstrapSmokeTests(unittest.TestCase):
    def test_packaged_bootstrap_can_migrate_and_apply_hotfixes(self):
        code = (
            "import os,tempfile,runpy; "
            "os.environ['DATA_DIR']=tempfile.mkdtemp(); "
            "runpy.run_path=lambda *a,**k: None; "
            "import bootstrap, webui; "
            "assert getattr(webui.WebControlServer, '_v333_updater_hotfix_applied', False); "
            "assert getattr(webui.WebControlServer, '_matrix_background_applied', False); "
            "assert getattr(webui.WebControlServer, '_prefix_blocks_applied', False)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ = "__main__":
    unittest.main()
