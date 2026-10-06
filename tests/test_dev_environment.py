import pathlib
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class DevelopmentEnvironmentContractTests(unittest.TestCase):
    def test_pyproject_requires_python_312(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(data["project"]["requires-python"], ">=3.12")

    def test_operator_wrappers_use_repo_python_runner(self):
        for rel in [
            "scripts/01-management-bootstrap.sh",
            "scripts/02-storage-bootstrap.sh",
            "scripts/03-fabric-bootstrap.sh",
            "scripts/04-validate.sh",
            "scripts/10-topology-select.sh",
            "scripts/20-model-reconcile.sh",
            "scripts/20-model-sync.sh",
            "scripts/25-runtime-reconcile.sh",
            "scripts/30-recipe-run.sh",
        ]:
            text = (ROOT / rel).read_text()
            self.assertIn("scripts/python.sh", text, rel)
            self.assertNotIn("exec python3 ", text, rel)

    def test_root_launchers_use_repo_python_runner(self):
        for rel in ["fleet", "fleetctl"]:
            text = (ROOT / rel).read_text()
            self.assertIn("scripts/python.sh", text)


if __name__ == "__main__":
    unittest.main()
