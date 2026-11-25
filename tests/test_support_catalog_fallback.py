"""Regression tests for packaged support-card-library catalog.json fallback resolution."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class SupportCatalogFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        resolved = str(PACER_BACKEND.resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)

    def _sample_catalog(self) -> dict:
        return {
            "schema": "uma.support_card_art.v1",
            "cards": [
                {
                    "card_id": 30173,
                    "type": "friend",
                    "name": "佐岳芽",
                    "image_status": "ready",
                }
            ],
        }

    def test_bridges_layout_resolves_catalog_when_pacer_runs_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            pacer_backend = app_root / "pacer" / "backend"
            plant_dst = pacer_backend / "core" / "autonomous_training" / "plant_support.py"
            plant_dst.parent.mkdir(parents=True, exist_ok=True)
            plant_dst.write_text(
                (PACER_BACKEND / "core" / "autonomous_training" / "plant_support.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            cut_dst = pacer_backend / "core" / "auto_trainer" / "cut_support_selection.py"
            cut_dst.parent.mkdir(parents=True, exist_ok=True)
            cut_dst.write_text(
                (PACER_BACKEND / "core" / "auto_trainer" / "cut_support_selection.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            bridge_cat = pacer_backend / "bridges" / "skills" / "backend" / "runs" / "support-card-library" / "catalog.json"
            bridge_cat.parent.mkdir(parents=True, exist_ok=True)
            bridge_cat.write_text(json.dumps(self._sample_catalog(), ensure_ascii=False), encoding="utf-8")

            plant_mod = _load_module("_test_plant_support_bridge", plant_dst)
            self.assertEqual(plant_mod.CATALOG.resolve(), bridge_cat.resolve())
            self.assertIn(30173, plant_mod.catalog())

            cut_mod = _load_module("_test_cut_support_bridge", cut_dst)
            with patch("core.cut_portable_runtime.portable_root", return_value=pacer_backend):
                cut_mod.support_api.cache_clear()
                api = cut_mod.support_api()
                self.assertEqual(api.CATALOG.resolve(), bridge_cat.resolve())
                self.assertIn(30173, api.catalog())

    def test_top_level_backend_runs_fallback_when_bridge_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            pacer_backend = app_root / "pacer" / "backend"
            # Place bridge leaf plant_support.py under pacer/backend/bridges/skills/backend/core/autonomous_training
            bridge_plant = pacer_backend / "bridges" / "skills" / "backend" / "core" / "autonomous_training" / "plant_support.py"
            bridge_plant.parent.mkdir(parents=True, exist_ok=True)
            bridge_plant.write_text(
                (PACER_BACKEND / "core" / "autonomous_training" / "plant_support.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            cut_dst = pacer_backend / "core" / "auto_trainer" / "cut_support_selection.py"
            cut_dst.parent.mkdir(parents=True, exist_ok=True)
            cut_dst.write_text(
                (PACER_BACKEND / "core" / "auto_trainer" / "cut_support_selection.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            top_cat = app_root / "backend" / "runs" / "support-card-library" / "catalog.json"
            top_cat.parent.mkdir(parents=True, exist_ok=True)
            top_cat.write_text(json.dumps(self._sample_catalog(), ensure_ascii=False), encoding="utf-8")

            plant_mod = _load_module("_test_plant_support_top", bridge_plant)
            self.assertEqual(plant_mod.CATALOG.resolve(), top_cat.resolve())
            self.assertIn(30173, plant_mod.catalog())

            cut_mod = _load_module("_test_cut_support_top", cut_dst)
            with patch("core.cut_portable_runtime.portable_root", return_value=pacer_backend):
                cut_mod.support_api.cache_clear()
                api = cut_mod.support_api()
                self.assertEqual(api.CATALOG.resolve(), top_cat.resolve())
                self.assertIn(30173, api.catalog())


if __name__ == "__main__":
    unittest.main()
