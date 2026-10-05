import importlib.util
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fleetctl", ROOT / "fleetctl.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def cfg2():
    return {
        "version": 2,
        "cluster": {
            "id": "dgx-c1", "ssh_user": "zbig",
            "fabric_primary": "192.168.100.0/24",
            "fabric_secondary": "192.168.101.0/24",
            "fabric_host_base": 10,
            "model_root": "/home/zbig/gguf",
        },
        "nodes": [
            {"index": 1, "management": "10.0.0.101"},
            {"index": 2, "management": "10.0.0.102"},
        ],
        "topologies": {"tp2": {"tp": 2, "nodes": [1,2]}},
    }


class SimpleFleetTests(unittest.TestCase):
    def test_stable_names(self):
        c=cfg2()
        self.assertEqual(f.mng_name(c,1),"mng-node1.dgx-c1")
        self.assertEqual(f.con_name(c,2),"con-node2.dgx-c1")
        self.assertEqual(f.con_name(c,2,2),"con2-node2.dgx-c1")

    def test_deterministic_fabric_addresses(self):
        c=cfg2()
        self.assertEqual(f.fabric_ip(c,1,1),"192.168.100.10")
        self.assertEqual(f.fabric_ip(c,2,1),"192.168.100.11")
        self.assertEqual(f.fabric_ip(c,2,2),"192.168.101.11")

    def test_hosts_block_contains_both_planes(self):
        text=f.hosts_block(cfg2())
        self.assertIn("10.0.0.101 mng-node1.dgx-c1",text)
        self.assertIn("192.168.100.11 con-node2.dgx-c1",text)
        self.assertIn("192.168.101.11 con2-node2.dgx-c1",text)

    def test_hosts_block_resolves_management_hostnames_to_ipv4(self):
        c=cfg2()
        c["nodes"][0]["management"]="spark-1"
        c["nodes"][1]["management"]="spark-2"
        def fake_getaddrinfo(host, *args, **kwargs):
            ip={"spark-1":"172.27.81.101","spark-2":"172.27.81.102"}[host]
            return [(f.socket.AF_INET, f.socket.SOCK_STREAM, 6, "", (ip, 0))]
        with mock.patch.object(f.socket,"getaddrinfo",side_effect=fake_getaddrinfo):
            text=f.hosts_block(c)
        self.assertIn("172.27.81.101 mng-node1.dgx-c1",text)
        self.assertIn("172.27.81.102 mng-node2.dgx-c1",text)
        self.assertNotIn("spark-1 mng-node1.dgx-c1",text)

    def test_docker_socket_group_command_discovers_group_by_gid(self):
        cmd=f._docker_socket_group_command()
        self.assertIn("stat -c %g /var/run/docker.sock",cmd)
        self.assertIn("getent group",cmd)
        self.assertNotIn("usermod -aG docker",cmd)

    def test_ensure_docker_access_adds_detected_group_then_rechecks_fresh_ssh(self):
        c=cfg2()
        results=[
            mock.Mock(returncode=0,stdout="container-runtime\n"),
            mock.Mock(returncode=1),
            mock.Mock(returncode=0),
        ]
        with mock.patch.object(f,"ssh",side_effect=results) as ssh_mock, \
             mock.patch.object(f,"is_local_address",return_value=False), \
             mock.patch.object(f,"run",return_value=mock.Mock(returncode=0)) as run_mock:
            self.assertTrue(f.ensure_docker_access(c,1))
        self.assertIn("usermod -aG container-runtime zbig",ssh_mock.call_args_list[2].args[2])
        verify=run_mock.call_args.args[0]
        self.assertEqual(verify[0],"ssh")
        self.assertIn("zbig@10.0.0.101",verify)

    def test_ensure_docker_access_does_not_modify_group_when_already_member(self):
        c=cfg2()
        results=[
            mock.Mock(returncode=0,stdout="docker\n"),
            mock.Mock(returncode=0),
        ]
        with mock.patch.object(f,"ssh",side_effect=results) as ssh_mock, \
             mock.patch.object(f,"is_local_address",return_value=False), \
             mock.patch.object(f,"run",return_value=mock.Mock(returncode=0)):
            self.assertTrue(f.ensure_docker_access(c,2))
        self.assertEqual(ssh_mock.call_count,2)

    def test_known_hosts_block_covers_management_and_both_fabric_aliases(self):
        c=cfg2()
        keys={
            1:[("ssh-ed25519","AAAA111=")],
            2:[("ssh-ed25519","AAAA222=")],
        }
        text=f.ssh_known_hosts_block(c,keys)
        self.assertIn("mng-node1.dgx-c1,con-node1.dgx-c1,con2-node1.dgx-c1,10.0.0.101,192.168.100.10,192.168.101.10 ssh-ed25519 AAAA111=",text)
        self.assertIn("mng-node2.dgx-c1,con-node2.dgx-c1,con2-node2.dgx-c1,10.0.0.102,192.168.100.11,192.168.101.11 ssh-ed25519 AAAA222=",text)

    def test_parse_host_public_keys_discards_comments_and_invalid_lines(self):
        parsed=f._parse_ssh_host_public_keys(
            "ssh-ed25519 AAAAC3NzYW1wbGU= root@node\n"
            "ecdsa-sha2-nistp256 BBBB+/== comment\n"
            "garbage line\n"
        )
        self.assertEqual(parsed,[
            ("ssh-ed25519","AAAAC3NzYW1wbGU="),
            ("ecdsa-sha2-nistp256","BBBB+/=="),
        ])

    def test_known_hosts_install_script_replaces_only_managed_block(self):
        script=f._known_hosts_install_script("dgx-c1","# managed\n")
        self.assertIn("~/.ssh/known_hosts",script)
        self.assertIn("BEGIN DGX-SPARK-FLEET dgx-c1 SSH-HOST-KEYS",script)
        self.assertIn("awk -v begin=",script)

    def test_known_hosts_block_covers_management_and_both_fabric_aliases(self):
        c=cfg2()
        keys={
            1:[("ssh-ed25519","AAAA111=")],
            2:[("ssh-ed25519","AAAA222=")],
        }
        text=f.ssh_known_hosts_block(c,keys)
        self.assertIn("mng-node1.dgx-c1,con-node1.dgx-c1,con2-node1.dgx-c1,10.0.0.101,192.168.100.10,192.168.101.10 ssh-ed25519 AAAA111=",text)
        self.assertIn("mng-node2.dgx-c1,con-node2.dgx-c1,con2-node2.dgx-c1,10.0.0.102,192.168.100.11,192.168.101.11 ssh-ed25519 AAAA222=",text)

    def test_parse_host_public_keys_discards_comments_and_invalid_lines(self):
        parsed=f._parse_ssh_host_public_keys(
            "ssh-ed25519 AAAAC3NzYW1wbGU= root@node\n"
            "ecdsa-sha2-nistp256 BBBB+/== comment\n"
            "garbage line\n"
        )
        self.assertEqual(parsed,[
            ("ssh-ed25519","AAAAC3NzYW1wbGU="),
            ("ecdsa-sha2-nistp256","BBBB+/=="),
        ])

    def test_known_hosts_install_script_replaces_only_managed_block(self):
        script=f._known_hosts_install_script("dgx-c1","# managed\n")
        self.assertIn("~/.ssh/known_hosts",script)
        self.assertIn("BEGIN DGX-SPARK-FLEET dgx-c1 SSH-HOST-KEYS",script)
        self.assertIn("awk -v begin=",script)


    def test_topology_parser_accepts_explicit_name_option(self):
        args=f.build_parser().parse_args(["topology","set","--cluster","dgx-c1","--name","tp2"])
        self.assertEqual(args.action,"set")
        self.assertEqual(args.cluster,"dgx-c1")
        self.assertEqual(args.name_opt,"tp2")

    def test_topology_parser_keeps_legacy_positional_name(self):
        args=f.build_parser().parse_args(["topology","set","--cluster","dgx-c1","tp2"])
        self.assertEqual(args.name,"tp2")

    def test_topology_validates_size(self):
        c=cfg2()
        self.assertEqual(f.topology(c,"tp2")["nodes"],[1,2])
        c["topologies"]["bad"]={"tp":3,"nodes":[1,2]}
        with self.assertRaises(SystemExit): f.topology(c,"bad")

    def test_four_node_presets_are_disjoint_for_prod_lab(self):
        c=cfg2()
        c["nodes"] += [{"index":3,"management":"10.0.0.103"},{"index":4,"management":"10.0.0.104"}]
        c["topologies"].update({"prod2":{"tp":2,"nodes":[1,2]},"lab2":{"tp":2,"nodes":[3,4]}})
        self.assertTrue(set(f.topology(c,"prod2")["nodes"]).isdisjoint(f.topology(c,"lab2")["nodes"]))

    def test_cluster_config_is_secret_free_shape(self):
        text=f.hosts_block(cfg2())
        for forbidden in ("PRIVATE KEY","password=","token=","ssh_identity"):
            self.assertNotIn(forbidden,text.lower())

    def test_model_path_must_stay_under_model_root(self):
        c=cfg2()
        self.assertEqual(f.checked_model_path(c,"/home/zbig/gguf/model-a"),"/home/zbig/gguf/model-a")
        self.assertEqual(f.checked_model_path(c,"/home/zbig/gguf/model-a/../model-b"),"/home/zbig/gguf/model-b")
        with self.assertRaises(SystemExit):
            f.checked_model_path(c,"/home/zbig/other/model-a")
        with self.assertRaises(SystemExit):
            f.checked_model_path(c,"relative/model-a")

    def test_storage_marker_binds_cluster_node_user_and_root(self):
        c=cfg2()
        self.assertEqual(f.storage_marker_expected(c,"dgx-c1",2),{
            "version":1,
            "cluster_id":"dgx-c1",
            "node_index":2,
            "ssh_user":"zbig",
            "model_root":"/home/zbig/gguf",
        })

    def test_model_root_must_be_absolute_and_not_root(self):
        c=cfg2()
        self.assertEqual(f.configured_model_root(c),"/home/zbig/gguf")
        c["cluster"]["model_root"]="relative/models"
        with self.assertRaises(SystemExit):
            f.configured_model_root(c)
        c["cluster"]["model_root"]="/"
        with self.assertRaises(SystemExit):
            f.configured_model_root(c)



    def test_hf_token_is_forwarded_over_stdin_not_command_line(self):
        c=cfg2()
        artifact={
            "kind":"huggingface",
            "repo":"org/private-model",
            "revision":"abc123",
            "mode":"full",
            "files":[],
        }
        result=mock.Mock(returncode=0)
        secret="hf_super_secret_test_token"
        with mock.patch.dict(os.environ,{"HF_TOKEN":secret},clear=False), \
             mock.patch.object(f,"ssh",return_value=result) as ssh_mock:
            f._download_artifact_on_node(c,1,artifact,"/home/zbig/gguf/hf/org/private-model")
        command=ssh_mock.call_args.args[2]
        kwargs=ssh_mock.call_args.kwargs
        self.assertNotIn(secret,command)
        self.assertIn("read -r HF_TOKEN",command)
        self.assertEqual(kwargs["input_text"],secret+"\n")
        self.assertNotIn("hf auth login",command)
        self.assertIn('"$HFV/bin/pip" install -U huggingface_hub',command)
        self.assertNotIn("hf_transfer",command)

    def test_hf_download_is_anonymous_when_management_token_is_absent(self):
        c=cfg2()
        artifact={"kind":"huggingface","repo":"org/public-model","revision":"","mode":"full","files":[]}
        result=mock.Mock(returncode=0)
        env=dict(os.environ)
        env.pop("HF_TOKEN",None)
        with mock.patch.dict(os.environ,env,clear=True), \
             mock.patch.object(f,"ssh",return_value=result) as ssh_mock:
            f._download_artifact_on_node(c,1,artifact,"/home/zbig/gguf/hf/org/public-model")
        command=ssh_mock.call_args.args[2]
        kwargs=ssh_mock.call_args.kwargs
        self.assertNotIn("read -r HF_TOKEN",command)
        self.assertIsNone(kwargs["input_text"])

    def test_hf_token_rejects_multiline_value(self):
        with mock.patch.dict(os.environ,{"HF_TOKEN":"hf_bad\nsecret"},clear=False):
            with self.assertRaises(SystemExit):
                f._management_hf_token()

    def test_model_reconcile_parser_defaults_to_all_recipes(self):
        args=f.build_parser().parse_args(["model-reconcile","--cluster","dgx-c1"])
        self.assertEqual(args.recipe,[])
        self.assertFalse(args.apply)
        self.assertFalse(args.download_missing)

    def test_model_reconcile_parser_accepts_recipe_subset(self):
        args=f.build_parser().parse_args([
            "model-reconcile","--cluster","dgx-c1",
            "--recipe","v41-exl3-vision","--recipe","v41-native-sglang","--apply"
        ])
        self.assertEqual(args.recipe,["v41-exl3-vision","v41-native-sglang"])
        self.assertTrue(args.apply)

    def test_canonical_hf_path_is_under_model_root(self):
        c=cfg2()
        a={"kind":"huggingface","repo":"deepseek-ai/DeepSeek-V4.1-Flash"}
        self.assertEqual(
            f.artifact_canonical_path(c,a),
            "/home/zbig/gguf/hf/deepseek-ai/DeepSeek-V4.1-Flash",
        )

    def test_all_cannot_be_mixed_with_explicit_recipe(self):
        with self.assertRaises(SystemExit):
            f.selected_recipe_records(["all","v41-exl3-vision"])

    def test_ensure_recipe_sources_initializes_only_missing_submodules(self):
        records=[
            (Path("a"), {"id":"v4-vision", "source":"sources/v4-vision-vllm"}),
            (Path("b"), {"id":"v41-exl3-vision", "source":"sources/v41-exl3-vllm"}),
        ]
        source1=f.ROOT / "sources" / "v4-vision-vllm"
        source2=f.ROOT / "sources" / "v41-exl3-vllm"
        run_result=mock.Mock(returncode=0)
        with mock.patch.object(f,"_source_initialized",side_effect=[False, True, True]) as state_mock, \
             mock.patch.object(f,"run",return_value=run_result) as run_mock:
            f.ensure_recipe_sources(records)
        cmd=run_mock.call_args.args[0]
        self.assertEqual(cmd[:4],["env","GIT_LFS_SKIP_SMUDGE=1","GIT_TERMINAL_PROMPT=0","git"])
        self.assertIn("submodule",cmd)
        self.assertIn("sources/v4-vision-vllm",cmd)
        self.assertNotIn("sources/v41-exl3-vllm",cmd)
        self.assertGreaterEqual(state_mock.call_count,3)

    def test_model_reconcile_bootstraps_recipe_sources_before_manifest(self):
        args=mock.Mock(cluster="dgx-c1", recipe=[], apply=False, download_missing=False)
        records=[(Path("r"), {"id":"v4-vision", "source":"sources/v4-vision-vllm"})]
        with mock.patch.object(f,"load_cfg",return_value=(cfg2(),Path("cfg"))), \
             mock.patch.object(f,"require_storage_ready"), \
             mock.patch.object(f,"selected_recipe_records",return_value=records), \
             mock.patch.object(f,"ensure_recipe_sources") as ensure_mock, \
             mock.patch.object(f,"merge_recipe_artifacts",return_value=[]) as merge_mock:
            with self.assertRaises(SystemExit):
                f.cmd_model_reconcile(args)
        ensure_mock.assert_called_once_with(records)
        merge_mock.assert_called_once_with(records)

    def test_artifact_merge_full_dominates_subset_and_adopts_revision(self):
        records=[
            (Path("a"),{"id":"exl3"}),
            (Path("b"),{"id":"native"}),
        ]
        manifests={
            "exl3":{"artifacts":[{
                "kind":"huggingface","repo":"deepseek-ai/DeepSeek-V4.1-Flash",
                "revision":"","mode":"subset","files":["47.safetensors","48.safetensors"],
                "legacy_names":["DeepSeek-V4.1-Flash"],
            }]},
            "native":{"artifacts":[{
                "kind":"huggingface","repo":"deepseek-ai/DeepSeek-V4.1-Flash",
                "revision":"abc123","mode":"full","expected_shards":48,
                "legacy_names":["DeepSeek-V4.1-Flash"],
            }]},
        }
        with mock.patch.object(f,"_artifact_provider_manifest",side_effect=lambda r: manifests[r["id"]]):
            got=f.merge_recipe_artifacts(records)
        self.assertEqual(len(got),1)
        self.assertEqual(got[0]["mode"],"full")
        self.assertEqual(got[0]["revision"],"abc123")
        self.assertEqual(got[0]["expected_shards"],48)
        self.assertEqual(got[0]["files"],[])
        self.assertEqual(got[0]["recipes"],["exl3","native"])


    def test_inventory_classifies_exact_legacy_directory_as_complete(self):
        c=cfg2()
        artifact={
            "kind":"huggingface","repo":"deepseek-ai/DeepSeek-V4.1-Flash",
            "revision":"","mode":"full","expected_shards":48,
            "legacy_names":["DeepSeek-V4.1-Flash"],
        }
        entry={
            "path":"/home/zbig/gguf/DeepSeek-V4.1-Flash","type":"directory",
            "basename":"DeepSeek-V4.1-Flash","files":["config.json"],
            "config":{},"repo_hints":[],"hf_commits":[],"has_config":True,
            "has_index":False,"index_expected":0,"index_present":0,
            "safetensors":48,"model_shards":48,"ggufs":0,"weights":48,"bytes":123,
        }
        got=f.classify_artifact_from_inventory(c,artifact,[entry])
        self.assertEqual(got["state"],"complete")
        self.assertEqual(got["path"],entry["path"])

    def test_inventory_marks_renamed_matching_shard_tree_as_candidate(self):
        c=cfg2()
        artifact={
            "kind":"huggingface","repo":"deepseek-ai/DeepSeek-V4.1-Flash",
            "revision":"","mode":"full","expected_shards":48,"legacy_names":[],
        }
        entry={
            "path":"/home/zbig/gguf/archive/checkpoint-current","type":"directory",
            "basename":"checkpoint-current","files":["config.json"],
            "config":{},"repo_hints":[],"hf_commits":[],"has_config":True,
            "has_index":False,"index_expected":0,"index_present":0,
            "safetensors":48,"model_shards":48,"ggufs":0,"weights":48,"bytes":456,
        }
        got=f.classify_artifact_from_inventory(c,artifact,[entry])
        self.assertEqual(got["state"],"candidate")
        self.assertIn("exact 48-shard structure",got["reasons"])

    def test_inventory_embedded_repo_id_proves_identity(self):
        c=cfg2()
        artifact={
            "kind":"huggingface","repo":"deepseek-ai/DeepSeek-V4-Flash-Vision-Exp",
            "revision":"","mode":"full","expected_shards":0,"legacy_names":[],
        }
        entry={
            "path":"/home/zbig/gguf/odd-name","type":"directory","basename":"odd-name",
            "files":["config.json","weights.safetensors"],"config":{},
            "repo_hints":["deepseek-ai/DeepSeek-V4-Flash-Vision-Exp"],"hf_commits":[],
            "has_config":True,"has_index":False,"index_expected":0,"index_present":0,
            "safetensors":1,"model_shards":0,"ggufs":0,"weights":1,"bytes":789,
        }
        got=f.classify_artifact_from_inventory(c,artifact,[entry])
        self.assertEqual(got["state"],"complete")
        self.assertIn("embedded Hugging Face repository id",got["reasons"])

    def test_inventory_mismatched_pinned_revision_is_candidate(self):
        c=cfg2()
        wanted="a"*40
        other="b"*40
        artifact={
            "kind":"huggingface","repo":"deepseek-ai/DeepSeek-V4.1-Flash",
            "revision":wanted,"mode":"full","expected_shards":48,
            "legacy_names":["DeepSeek-V4.1-Flash"],
        }
        entry={
            "path":"/home/zbig/gguf/DeepSeek-V4.1-Flash","type":"directory",
            "basename":"DeepSeek-V4.1-Flash","files":["config.json"],
            "config":{},"repo_hints":[],"hf_commits":[other],"has_config":True,
            "has_index":False,"index_expected":0,"index_present":0,
            "safetensors":48,"model_shards":48,"ggufs":0,"weights":48,"bytes":123,
        }
        got=f.classify_artifact_from_inventory(c,artifact,[entry])
        self.assertEqual(got["state"],"candidate")
        self.assertIn("revision differs", " ".join(got["reasons"]))

    def test_remote_inventory_exposes_renamed_hf_tree_and_standalone_gguf(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            model=root/"renamed"
            model.mkdir()
            (model/"config.json").write_text('{"_name_or_path":"deepseek-ai/DeepSeek-V4.1-Flash"}')
            (model/"model-00001-of-00002.safetensors").write_bytes(b"x")
            (model/"model-00002-of-00002.safetensors").write_bytes(b"y")
            (root/"other.Q4_K_M.gguf").write_bytes(b"gguf")
            r=subprocess.run(["python3","-",str(root)],input=f.REMOTE_MODEL_INVENTORY,
                             text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
            entries=json.loads(r.stdout)["entries"]
        paths={Path(e["path"]).name:e for e in entries}
        self.assertIn("renamed",paths)
        self.assertEqual(paths["renamed"]["model_shards"],2)
        self.assertEqual(paths["renamed"]["config"]["_name_or_path"],"deepseek-ai/DeepSeek-V4.1-Flash")
        self.assertIn("other.Q4_K_M.gguf",paths)
        self.assertEqual(paths["other.Q4_K_M.gguf"]["type"],"gguf")

    def test_similar_named_standalone_gguf_does_not_match_hf_directory_artifact(self):
        c=cfg2()
        artifact={
            "kind":"huggingface","format":"safetensors-directory",
            "repo":"deepseek-ai/DeepSeek-V4-Flash-Vision-Exp",
            "revision":"","mode":"full","expected_shards":0,
            "legacy_names":["DeepSeek-V4-Flash-Vision-Exp"],
        }
        entry={
            "path":"/home/zbig/gguf/DeepSeek-V4-Flash-Vision-Exp-IQ2XXS.gguf",
            "type":"gguf","basename":"DeepSeek-V4-Flash-Vision-Exp-IQ2XXS.gguf",
            "files":["DeepSeek-V4-Flash-Vision-Exp-IQ2XXS.gguf"],
            "config":{},"repo_hints":[],"hf_commits":[],"has_config":False,
            "has_index":False,"index_expected":0,"index_present":0,
            "safetensors":0,"model_shards":0,"ggufs":1,"weights":1,"bytes":123,
        }
        got=f.classify_artifact_from_inventory(c,artifact,[entry])
        self.assertEqual(got["state"],"missing")

    def test_exact_named_gguf_only_directory_does_not_match_hf_directory_artifact(self):
        c=cfg2()
        artifact={
            "kind":"huggingface","format":"safetensors-directory",
            "repo":"deepseek-ai/DeepSeek-V4-Flash-Vision-Exp",
            "revision":"","mode":"full","expected_shards":0,
            "legacy_names":["DeepSeek-V4-Flash-Vision-Exp"],
        }
        entry={
            "path":"/home/zbig/gguf/DeepSeek-V4-Flash-Vision-Exp",
            "type":"directory","basename":"DeepSeek-V4-Flash-Vision-Exp",
            "files":["config.json","model.gguf"],"config":{},
            "repo_hints":[],"hf_commits":[],"has_config":True,
            "has_index":False,"index_expected":0,"index_present":0,
            "safetensors":0,"model_shards":0,"ggufs":1,"weights":1,"bytes":456,
        }
        got=f.classify_artifact_from_inventory(c,artifact,[entry])
        self.assertEqual(got["state"],"missing")

    def test_artifact_identity_includes_format(self):
        a={"kind":"huggingface","repo":"org/model","format":"safetensors-directory"}
        b={"kind":"huggingface","repo":"org/model","format":"gguf-bundle"}
        self.assertNotEqual(f._artifact_identity(a),f._artifact_identity(b))

    def test_unresolved_candidates_are_reported_for_download_guard(self):
        plan=[{"artifact":{"repo":"org/model"},"source":None,"probes":{
            1:{"state":"candidate","path":"/models/x"},2:{"state":"missing"}
        }}]
        got=f.unresolved_candidate_entries(plan)
        self.assertEqual(len(got),1)
        self.assertEqual(got[0][1],1)

    def test_choose_download_seed_prefers_most_complete_partial(self):
        entry={"probes":{
            1:{"state":"partial","present":3},
            2:{"state":"partial","present":17},
            3:{"state":"missing","present":0},
        }}
        self.assertEqual(f._choose_download_seed(entry),2)
        self.assertEqual(f._choose_download_seed(entry,1),1)


    def test_management_iface_uses_managed_peer_alias_not_raw_bootstrap_name(self):
        c=cfg2()
        c["nodes"][0]["management"]="spark-1"
        c["nodes"][1]["management"]="spark-2"
        result=mock.Mock(returncode=0,stdout="172.27.81.102 dev enP7s7 src 172.27.81.101\n")
        with mock.patch.object(f,"ssh",return_value=result) as ssh_mock:
            self.assertEqual(f.management_iface(c,1),"enP7s7")
        self.assertIn("ip route get mng-node2.dgx-c1",ssh_mock.call_args.args[2])
        self.assertNotIn("spark-2",ssh_mock.call_args.args[2])

    def test_recipe_runtime_env_maps_exl3_to_canonical_reconciled_paths(self):
        c=cfg2()
        profile={"adapter":"mia_exl3"}
        artifacts=[
            {"kind":"huggingface","format":"safetensors-directory","repo":"Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw","mode":"full"},
            {"kind":"huggingface","format":"safetensors-directory","repo":"deepseek-ai/DeepSeek-V4.1-Flash","mode":"subset"},
        ]
        env=f._recipe_runtime_env(c,profile,artifacts)
        self.assertEqual(env["DGX_MODEL_HOST"],"/home/zbig/gguf/hf/Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw")
        self.assertEqual(env["DGX_ENGRAM_DIR"],"/home/zbig/gguf/hf/deepseek-ai/DeepSeek-V4.1-Flash")
        self.assertEqual(env["DGX_WORKER_MODEL_DIR"],env["DGX_MODEL_HOST"])
        self.assertEqual(env["DGX_RECONCILED_MODELS"],"1")

    def test_recipe_runtime_env_maps_v4_parent_directory(self):
        c=cfg2()
        profile={"adapter":"v4_launcher"}
        artifacts=[{"kind":"huggingface","format":"safetensors-directory","repo":"deepseek-ai/DeepSeek-V4-Flash-Vision-Exp","mode":"full"}]
        env=f._recipe_runtime_env(c,profile,artifacts)
        self.assertEqual(env["DGX_V4_MODELS_HOST"],"/home/zbig/gguf/hf/deepseek-ai")



if __name__ == "__main__": unittest.main()
