import importlib.util
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fleet", ROOT / "fleet.py")
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)


class FleetTests(unittest.TestCase):
    def test_merge_env_replaces_and_appends(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ex = td / "example"
            out = td / "env"
            ex.write_text("A=1\n# C\nB=two\n")
            fleet.merge_env(ex, out, {"A": "9", "C": "hello world"})
            text = out.read_text()
            self.assertIn("A=9", text)
            self.assertIn("B=two", text)
            self.assertIn("C='hello world'", text)

    def test_netplan_only_contains_roce(self):
        cluster = {"cluster": {"roce_mtu": 9000}}
        node = {
            "name": "spark1",
            "roce": [
                {"ifname": "enp1", "ibdev": "roce0", "address": "192.168.192.10/24"},
                {"ifname": "enp2", "ibdev": "roce1", "address": "192.168.193.10/24"},
            ],
        }
        text = fleet.render_netplan(cluster, node)
        self.assertIn("enp1:", text)
        self.assertIn("enp2:", text)
        self.assertIn("mtu: 9000", text)
        self.assertNotIn("gateway", text)

    def test_replace_assignment(self):
        text = 'MASTER_ADDR="1.2.3.4"\nPORT="8888"\n'
        text = fleet.replace_assignment(text, "MASTER_ADDR", "192.168.1.1")
        self.assertIn("MASTER_ADDR=192.168.1.1", text)

    def test_ip_helpers(self):
        self.assertEqual(fleet.ip_only("192.168.192.10/24"), "192.168.192.10")
        self.assertEqual(fleet.net_only("192.168.192.10/24"), "192.168.192.0/24")


    def test_named_cluster_inventory_sets_identity(self):
        old_root, old_clusters = fleet.ROOT, fleet.CLUSTERS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                fleet.ROOT = td
                fleet.CLUSTERS_DIR = td / "config" / "clusters"
                (td / "config").mkdir()
                (td / "config" / "cluster.example.toml").write_text(
                    'version = 1\n\n[cluster]\nssh_user = "nvidia"\n'
                )
                out = fleet.write_cluster_inventory(fleet.CLUSTERS_DIR / "lab-a.toml", "lab-a")
                cfg = fleet.load_toml(out)
                self.assertEqual(cfg["cluster"]["id"], "lab-a")
        finally:
            fleet.ROOT, fleet.CLUSTERS_DIR = old_root, old_clusters

    def test_active_profile_is_isolated_per_cluster(self):
        old_state, old_root = fleet.STATE, fleet.STATE_ROOT
        try:
            with tempfile.TemporaryDirectory() as td:
                fleet.STATE_ROOT = Path(td)
                fleet.set_runtime_state("lab-a")
                fleet.set_active("model-a")
                fleet.set_runtime_state("lab-b")
                self.assertIsNone(fleet.active_profile_name())
                fleet.set_active("model-b")
                fleet.set_runtime_state("lab-a")
                self.assertEqual(fleet.active_profile_name(), "model-a")
                fleet.set_runtime_state("lab-b")
                self.assertEqual(fleet.active_profile_name(), "model-b")
        finally:
            fleet.STATE, fleet.STATE_ROOT = old_state, old_root

    def test_cluster_identity_mismatch_is_rejected(self):
        cluster = {
            "cluster": {"id": "wrong"},
            "nodes": {"spark1": {"local": True, "roce": []}},
        }
        with self.assertRaises(SystemExit):
            fleet.validate_cluster_inventory(cluster, "expected", Path("expected.toml"))

    def test_v4_launcher_render_fixture(self):
        old_root, old_state = fleet.ROOT, fleet.STATE
        try:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                fleet.ROOT = td
                fleet.STATE = td / ".state"
                srcroot = td / "sources" / "v4"
                (srcroot / "launchers").mkdir(parents=True)
                (srcroot / "launchers" / "l.sh").write_text(
                    '#!/bin/bash\n'
                    'NODE_RANK="$1"\n'
                    'IMAGE="old"\nNAME="c"\nMASTER_ADDR="1.1.1.1"\nMASTER_PORT="1"\nPORT="2"\n'
                    'case "$NODE_RANK" in\n  0) HOST_IP=1; HEADLESS=""; MODELS_HOST="/a" ;;;;\n  1) HOST_IP=2; HEADLESS="--headless"; MODELS_HOST="/b" ;;;;\n  *) exit 2 ;;;;\nesac\n'
                    'docker run -e NCCL_IB_HCA=oldib \\n'
                    '  -e NCCL_SOCKET_IFNAME=oldif -e GLOO_SOCKET_IFNAME=oldif -e TP_SOCKET_IFNAME=oldif \\n'
                    '  -e NCCL_IB_MERGE_NICS=0 x\n'
                )
                cluster={
                    "cluster": {},
                    "topologies": {"tp2":{"nodes":["spark1","spark2"]}},
                    "nodes": {
                        "spark1":{"local":True,"model_host":"/m1","roce":[{"ifname":"if1","ibdev":"ib1","address":"192.168.1.10/24"}]},
                        "spark2":{"model_host":"/m2","roce":[{"ifname":"if2","ibdev":"ib2","address":"192.168.1.11/24"}]},
                    }
                }
                profile={"id":"p","adapter":"v4_launcher","source":"sources/v4","topology":"tp2","launcher":"launchers/l.sh","image":"new-image","master_port":55,"port":8888}
                out=fleet.render_v4_launcher(cluster, profile).read_text()
                self.assertIn("MASTER_ADDR=192.168.1.10", out)
                self.assertIn("FLEET_NCCL_IB_HCA=ib1", out)
                self.assertIn("FLEET_NCCL_IB_HCA=ib2", out)
                self.assertIn('NCCL_IB_HCA="$FLEET_NCCL_IB_HCA"', out)
                self.assertIn('NCCL_SOCKET_IFNAME="$FLEET_SOCKET_IF"', out)
        finally:
            fleet.ROOT, fleet.STATE = old_root, old_state


if __name__ == "__main__":
    unittest.main()
