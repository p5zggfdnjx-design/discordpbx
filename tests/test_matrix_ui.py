from __future__ import annotations

import unittest

from matrix_ui import MATRIX_UI_SCRIPT, inject_matrix_ui


class MatrixUiTests(unittest.TestCase):
    def test_matrix_background_replaces_grid_style_and_injects_once(self):
        page = "<html><body><main>PBX</main></body></html>"
        once = inject_matrix_ui(page)
        twice = inject_matrix_ui(once)
        self.assertEqual(once, twice)
        self.assertEqual(once.count('pbx-matrix-background-script'), 1)
        self.assertIn('id="matrixRain"', once)
        self.assertIn("01ABCDEFGHIJKLMNOPQRSTUVWXYZ", once)
        self.assertIn("background-image:none!important", once)
        self.assertIn("prefers-reduced-motion", MATRIX_UI_SCRIPT)


if __name__ == "__main__":
    unittest.main()
