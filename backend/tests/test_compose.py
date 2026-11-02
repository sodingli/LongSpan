from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.media import compose as compose_mod


class RemotionBinTest(unittest.TestCase):
    def test_missing_dependency_has_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(compose_mod, "REMOTION_DIR", Path(tmp)):
                with self.assertRaisesRegex(RuntimeError, "bash install.sh remotion"):
                    compose_mod._remotion_bin()

    def test_uses_project_local_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_path = Path(tmp) / "node_modules" / ".bin" / "remotion"
            bin_path.parent.mkdir(parents=True)
            bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(bin_path, 0o755)

            with patch.object(compose_mod, "REMOTION_DIR", Path(tmp)):
                self.assertEqual(compose_mod._remotion_bin(), str(bin_path))


if __name__ == "__main__":
    unittest.main()
