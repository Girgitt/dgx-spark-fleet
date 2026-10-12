import importlib.util
import os
import tempfile
from pathlib import Path
import unittest
from unittest import mock

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

    def test_exl3_configuration_uses_reconciled_model_paths_and_disables_download(self):
        old_root=fleet.ROOT
        try:
            with tempfile.TemporaryDirectory() as td:
                td=Path(td)
                fleet.ROOT=td
                src=td/"sources"/"exl3"
                src.mkdir(parents=True)
                (src/".env.example").write_text("HEAD_IP=x\nWORKER_IP=y\nAUTO_DOWNLOAD=1\n")
                cluster={
                    "cluster":{"ssh_user":"admin"},
                    "topologies":{"tp2":{"nodes":["node1","node2"]}},
                    "nodes":{
                        "node1":{"local":True,"roce":[{"ifname":"if1","ibdev":"ib1","address":"10.1.0.1/24"}]},
                        "node2":{"ssh_host":"mng-node2","roce":[{"ifname":"if2","ibdev":"ib2","address":"10.1.0.2/24"}]},
                    },
                }
                profile={"id":"p","adapter":"mia_exl3","source":"sources/exl3","topology":"tp2","env_example":".env.example","env_file":".env","env":{}}
                env={
                    "DGX_RECONCILED_MODELS":"1",
                    "DGX_MODEL_HOST":"/models/exl3",
                    "DGX_ENGRAM_DIR":"/models/native",
                    "DGX_WORKER_MODEL_DIR":"/models/exl3",
                    "DGX_WORKER_ENGRAM_DIR":"/models/native",
                }
                with mock.patch.dict(os.environ,env,clear=False):
                    out=fleet.configure_mia_exl3(cluster,profile).read_text()
                self.assertIn("MODEL_HOST=/models/exl3",out)
                self.assertIn("ENGRAM_DIR=/models/native",out)
                self.assertIn("WORKER_MODEL_DIR=/models/exl3",out)
                self.assertIn("AUTO_DOWNLOAD=0",out)
        finally:
            fleet.ROOT=old_root


    def test_exl3_configuration_maps_cooperative_runtime_overrides(self):
        old_root=fleet.ROOT
        try:
            with tempfile.TemporaryDirectory() as td:
                td=Path(td); fleet.ROOT=td
                src=td/"sources"/"exl3"; src.mkdir(parents=True)
                (src/".env.example").write_text("HEAD_IP=x\nWORKER_IP=y\n")
                cluster={
                    "cluster":{"ssh_user":"admin"},
                    "topologies":{"tp2":{"nodes":["node1","node2"]}},
                    "nodes":{
                        "node1":{"local":True,"roce":[{"ifname":"if1","ibdev":"ib1","address":"10.1.0.1/24"}]},
                        "node2":{"ssh_host":"mng-node2","roce":[{"ifname":"if2","ibdev":"ib2","address":"10.1.0.2/24"}]},
                    },
                }
                profile={"id":"p","adapter":"mia_exl3","source":"sources/exl3","topology":"tp2","env_example":".env.example","env_file":".env","env":{}}
                env={
                    "DGX_COOP_OVERLAY_HOST":"/home/admin/.cache/vllm-dsv41-flash-exl3/coop/abc/exl3-cooperative.py",
                    "DGX_COOP_TEMP_ROWS_FUSED":"8",
                    "DGX_COOP_IMAGE":"ghcr.io/example/image@sha256:abc",
                }
                with mock.patch.dict(os.environ,env,clear=False):
                    out=fleet.configure_mia_exl3(cluster,profile).read_text()
                self.assertIn("EXL3_OVERLAY_HOST=/home/admin/.cache/vllm-dsv41-flash-exl3/coop/abc/exl3-cooperative.py",out)
                self.assertIn("EXL3_TEMP_ROWS_FUSED=8",out)
                self.assertIn("IMAGE=ghcr.io/example/image@sha256:abc",out)
        finally:
            fleet.ROOT=old_root



    def test_reconciled_exl3_builds_slim_engram_view_with_skip_sync(self):
        import json
        import shutil
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            source = td / "native"
            source.mkdir()
            (source / "config.json").write_text('{"architectures": ["DeepseekV41ForCausalLM"]}')
            index = {"weight_map": {
                "model.layers.1.engram.embed.weight": "model-00047-of-00048.safetensors",
                "model.layers.14.engram.embed.scale": "model-00048-of-00048.safetensors",
                "model.layers.0.mlp.experts.weight": "model-00001-of-00048.safetensors",
            }}
            (source / "model.safetensors.index.json").write_text(json.dumps(index))
            for name in ("model-00047-of-00048.safetensors", "model-00048-of-00048.safetensors"):
                (source / name).write_bytes(b"placeholder-shard")
            upstream = td / "sources" / "exl3"
            (upstream / "scripts").mkdir(parents=True)
            shutil.copy2(
                ROOT / "sources/v41-exl3-vllm/scripts/prepare_engram_src.py",
                upstream / "scripts/prepare_engram_src.py",
            )
            slim = td / "cache" / "engram-src"
            profile = {"adapter": "mia_exl3", "env": {
                "SKIP_SYNC": "1", "ENGRAM_SRC": str(slim),
            }}
            env = {"DGX_RECONCILED_MODELS": "1", "DGX_ENGRAM_DIR": str(source)}
            with mock.patch.dict(os.environ, env):
                fleet.prepare_reconciled_exl3_engram(profile, upstream)
            self.assertEqual(
                sorted(json.loads((slim / "model.safetensors.index.json").read_text())["weight_map"]),
                ["model.layers.1.engram.embed.weight", "model.layers.14.engram.embed.scale"],
            )
            self.assertEqual((slim / "config.json").read_bytes(), (source / "config.json").read_bytes())
            self.assertEqual(
                (slim / "model-00047-of-00048.safetensors").stat().st_ino,
                (source / "model-00047-of-00048.safetensors").stat().st_ino,
            )
            # A second prepare (or start) must be idempotent.
            with mock.patch.dict(os.environ, env):
                fleet.prepare_reconciled_exl3_engram(profile, upstream)

    def test_reconciled_exl3_rejects_overwriting_native_source(self):
        with tempfile.TemporaryDirectory() as td:
            env = {"DGX_RECONCILED_MODELS": "1", "DGX_ENGRAM_DIR": td}
            with mock.patch.dict(os.environ, env):
                with self.assertRaisesRegex(SystemExit, "must differ"):
                    fleet.reconciled_exl3_engram_paths({"env": {"ENGRAM_SRC": td}})

    def test_exl3_start_prepares_slim_view_before_upstream_start(self):
        events = []
        profile = {"adapter": "mia_exl3"}
        with mock.patch.object(fleet, "configure_profile", side_effect=lambda *a: events.append("configure")):
            with mock.patch.object(fleet, "ensure_source", return_value=Path("/tmp/src")):
                with mock.patch.object(fleet, "prepare_reconciled_exl3_engram", side_effect=lambda *a: events.append("slim")):
                    with mock.patch.object(fleet, "run", side_effect=lambda *a, **kw: events.append("start")):
                        fleet.start_profile({}, profile)
        self.assertEqual(events, ["configure", "slim", "start"])

    def test_exl3_prepare_stages_slim_even_when_download_skipped(self):
        events = []
        profile = {"adapter": "mia_exl3"}
        with mock.patch.dict(os.environ, {"DGX_RECONCILED_MODELS": "1"}):
            with mock.patch.object(fleet, "configure_profile", side_effect=lambda *a: events.append("configure")):
                with mock.patch.object(fleet, "ensure_source", return_value=Path("/tmp/src")):
                    with mock.patch.object(fleet, "prepare_reconciled_exl3_engram", side_effect=lambda *a: events.append("slim")):
                        with mock.patch.object(fleet, "run", side_effect=lambda *a, **kw: events.append("download")):
                            fleet.prepare_profile({}, profile)
        self.assertEqual(events, ["configure", "slim"])


if __name__ == "__main__":
    unittest.main()
