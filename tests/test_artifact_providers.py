import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ROOT / "recipes" / "artifacts"


class ArtifactProviderTests(unittest.TestCase):
    def run_provider(self, name, source):
        r = subprocess.run(
            [str(PROVIDERS / name), "--source", str(source), "--recipe-id", "test-recipe"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        return json.loads(r.stdout)

    def test_mia_exl3_reads_current_recipe_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / ".env.example").write_text(
                "HF_MODEL_REPO=Org/Model-X\n"
                "HF_ENGRAM_REPO=Org/Native-X\n"
                "EXPECTED_SHARDS=39\n"
            )
            (d / "download.sh").write_text(
                'ENGRAM_FILES=("model-00047-of-00048.safetensors" '
                '"model-00048-of-00048.safetensors" '
                '"model.safetensors.index.json" "config.json")\n'
            )
            got = self.run_provider("mia_exl3.py", d)
        self.assertEqual(got["artifacts"][0]["repo"], "Org/Model-X")
        self.assertEqual(got["artifacts"][0]["expected_shards"], 39)
        self.assertEqual(got["artifacts"][1]["mode"], "subset")
        self.assertIn("model-00047-of-00048.safetensors", got["artifacts"][1]["files"])

    def test_mia_sglang_reads_pinned_repo_revision(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "start.sh").write_text(
                'HF_REPO="${HF_REPO:-deepseek-ai/DeepSeek-V4.1-Flash}"\n'
                'HF_REVISION="${HF_REVISION:-abc123}"\n'
                'EXPECTED_SHARDS="${EXPECTED_SHARDS:-48}"\n'
            )
            got = self.run_provider("mia_sglang.py", d)
        art = got["artifacts"][0]
        self.assertEqual(art["repo"], "deepseek-ai/DeepSeek-V4.1-Flash")
        self.assertEqual(art["revision"], "abc123")
        self.assertEqual(art["expected_shards"], 48)

    def test_v4_vision_reads_model_repo_revision_and_legacy_name(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "vision-exp").mkdir()
            (d / "launchers").mkdir()
            (d / "README.md").write_text(
                "Model deepseek-ai/DeepSeek-V4-Flash-Vision-Exp "
                "(checkpoint pinned at commit `0123456789abcdef0123456789abcdef01234567`).\n"
            )
            (d / "vision-exp" / "README.md").write_text("deepseek-ai/DeepSeek-V4-Flash-Vision-Exp\n")
            (d / "launchers" / "ds4-vision-tp2.sh").write_text(
                'MODEL_DIR="${MODEL_DIR:-DeepSeek-V4-Flash-Vision-Exp}"\n'
            )
            got = self.run_provider("v4_vision.py", d)
        art = got["artifacts"][0]
        self.assertEqual(art["repo"], "deepseek-ai/DeepSeek-V4-Flash-Vision-Exp")
        self.assertEqual(art["revision"], "0123456789abcdef0123456789abcdef01234567")
        self.assertIn("DeepSeek-V4-Flash-Vision-Exp", art["legacy_names"])


if __name__ == "__main__":
    unittest.main()
