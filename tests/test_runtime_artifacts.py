import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PROVIDER = ROOT / "recipes" / "runtime_artifacts" / "v41_exl3_coop.py"
spec = importlib.util.spec_from_file_location("v41_exl3_coop", PROVIDER)
coop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coop)


class RuntimeArtifactProviderTests(unittest.TestCase):
    def test_provider_emits_exact_cooperative_moe_pins(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td)
            (source / "overlay").mkdir()
            (source / "overlay" / "exl3.py").write_text("stock fixture")
            with mock.patch.object(coop, "sha256", return_value=coop.STOCK_SHA), \
                 mock.patch("sys.argv", [
                     str(PROVIDER), "--source", str(source), "--recipe-id", "v41-exl3-vision-coop"
                 ]), \
                 mock.patch("builtins.print") as print_mock:
                coop.main()
        data = json.loads(print_mock.call_args.args[0])
        artifact = data["runtime_artifacts"][0]
        self.assertEqual(artifact["sha256"], coop.BINARY_SHA)
        self.assertEqual(artifact["runtime_py"]["sha256"], coop.RUNTIME_SHA)
        self.assertEqual(artifact["source"]["ref"], coop.SOURCE_REF)
        self.assertEqual(artifact["image"]["reference"], coop.IMAGE_REF)
        self.assertEqual(artifact["image"]["digest"], coop.IMAGE_DIGEST)
        self.assertEqual(
            artifact["image"]["legacy_config_id"], coop.IMAGE_LEGACY_CONFIG_ID
        )
        self.assertEqual(artifact["gate"]["checks"], 54)

    def test_provider_rejects_incompatible_stock_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td)
            (source / "overlay").mkdir()
            (source / "overlay" / "exl3.py").write_text("not the pinned overlay")
            with mock.patch("sys.argv", [
                str(PROVIDER), "--source", str(source), "--recipe-id", "v41-exl3-vision-coop"
            ]):
                with self.assertRaises(SystemExit):
                    coop.main()


if __name__ == "__main__":
    unittest.main()
