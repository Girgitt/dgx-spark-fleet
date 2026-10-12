import importlib.util
import json
import os
import subprocess
import sys
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
            r=subprocess.run([sys.executable,"-",str(root)],input=f.REMOTE_MODEL_INVENTORY,
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

    def test_reconcile_plan_ignores_candidate_path_claimed_by_other_artifact(self):
        c=cfg2()
        native_rev="f"*40
        exl3={
            "kind":"huggingface","format":"safetensors-directory",
            "repo":"Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw",
            "revision":"","mode":"full","expected_shards":39,
            "legacy_names":["DeepSeek-V4.1-Flash-EXL3-2.9bpw"],"recipes":["exl3"],
        }
        native={
            "kind":"huggingface","format":"safetensors-directory",
            "repo":"deepseek-ai/DeepSeek-V4.1-Flash",
            "revision":native_rev,"mode":"full","expected_shards":48,
            "legacy_names":["DeepSeek-V4.1-Flash"],"recipes":["native"],
        }
        exl3_path="/home/zbig/gguf/hf/Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw"
        native_path="/home/zbig/gguf/hf/deepseek-ai/DeepSeek-V4.1-Flash"
        exl3_entry={
            "path":exl3_path,"type":"directory","basename":"DeepSeek-V4.1-Flash-EXL3-2.9bpw",
            "files":["config.json"],"config":{},
            # Derived EXL3 checkpoints may retain the upstream model identity.
            "repo_hints":["deepseek-ai/DeepSeek-V4.1-Flash"],
            "hf_commits":["e"*40],"has_config":True,"has_index":False,
            "index_expected":0,"index_present":0,"safetensors":39,"model_shards":39,
            "ggufs":0,"weights":39,"bytes":123,
        }
        native_partial={
            "path":native_path,"type":"directory","basename":"DeepSeek-V4.1-Flash",
            "files":["config.json"],"config":{},"repo_hints":[],"hf_commits":[],
            "has_config":True,"has_index":False,"index_expected":0,"index_present":0,
            "safetensors":26,"model_shards":26,"ggufs":0,"weights":26,"bytes":456,
        }
        inventories={1:[exl3_entry,native_partial],2:[exl3_entry]}
        plan,_=f.build_reconcile_plan(c,[exl3,native],inventories)
        native_plan=plan[1]
        self.assertEqual(native_plan["probes"][1]["state"],"partial")
        self.assertEqual(native_plan["probes"][2]["state"],"missing")
        self.assertEqual(native_plan["probes"][2]["path"],native_path)
        ignored=native_plan["probes"][2]["ignored_candidate"]
        self.assertEqual(ignored["path"],exl3_path)
        self.assertEqual(ignored["owner_repos"],[exl3["repo"]])
        self.assertEqual(native_plan["candidates"],[])
        self.assertEqual(f._choose_download_seed(native_plan),1)

    def test_unrelated_vision_checkpoint_does_not_block_engrams_subset_sync(self):
        """dgx-c2: V4 Vision has the four files but is NOT native V4.1."""
        cfg=cfg2()
        base="/home/zbig/gguf/hf/"
        exl3_path=base+"Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw"
        native_path=base+"deepseek-ai/DeepSeek-V4.1-Flash"
        vision_path=base+"deepseek-ai/DeepSeek-V4-Flash-Vision-Exp"
        subset=["config.json", "model.safetensors.index.json",
                "model-00047-of-00048.safetensors", "model-00048-of-00048.safetensors"]
        exl3={"kind":"huggingface","format":"safetensors-directory",
              "repo":"Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw",
              "mode":"full","expected_shards":39,
              "legacy_names":["DeepSeek-V4.1-Flash-EXL3-2.9bpw"]}
        native={"kind":"huggingface","format":"safetensors-directory",
                "repo":"deepseek-ai/DeepSeek-V4.1-Flash",
                "mode":"subset","files":subset,
                "legacy_names":["DeepSeek-V4.1-Flash"]}
        def entry(path, files, shards, *, hints=None):
            return {"path":path,"type":"directory","basename":path.rsplit("/",1)[-1],
                    "files":files,"config":{},"repo_hints":hints or [],"hf_commits":[],
                    "has_config":True,"has_index":True,
                    "index_expected":shards,"index_present":shards,
                    "safetensors":shards,"model_shards":shards,"ggufs":0,
                    "weights":shards,"bytes":1234}
        exl3_tree=entry(exl3_path,["config.json"],39)
        native_tree=entry(native_path,subset,48)
        # V4 Vision contains the four commonly named files, but no native
        # V4.1 repository identity and no canonical/legacy name match.
        vision_tree=entry(vision_path,subset,48)
        solo=f.classify_artifact_from_inventory(cfg,native,[vision_tree])
        self.assertEqual(solo["state"],"missing")
        self.assertEqual(solo["path"],native_path)
        plan,inventory=f.build_reconcile_plan(cfg,[exl3,native],{
            1:[exl3_tree,native_tree,vision_tree],
            2:[vision_tree],
        })
        self.assertEqual(plan[0]["probes"][2]["state"],"missing")
        self.assertEqual(plan[1]["probes"][2]["state"],"missing")
        self.assertEqual(plan[1]["probes"][2]["path"],native_path)
        self.assertEqual(plan[1]["source"],1)
        self.assertFalse(plan[1]["candidates"])
        # Inventory retains foreign models as UNMATCHED, not delete targets.
        self.assertEqual(inventory[2][0]["path"],vision_path)

    def test_subset_metadata_alone_cannot_create_a_candidate(self):
        cfg=cfg2()
        native={"kind":"huggingface","repo":"deepseek-ai/DeepSeek-V4.1-Flash",
                "mode":"subset","files":["config.json","model.safetensors.index.json"]}
        other={"path":"/home/zbig/gguf/hf/org/completely-different-model",
               "basename":"completely-different-model","type":"directory",
               "files":["config.json","model.safetensors.index.json"],
               "has_config":True,"has_index":True,"index_expected":0,"index_present":0,
               "config":{},"repo_hints":[],"hf_commits":[],
               "safetensors":0,"model_shards":0,"weights":0,"ggufs":0,"bytes":10}
        self.assertEqual(f.classify_artifact_from_inventory(cfg,native,[other])["state"],
                         "missing")
        # Independent identity evidence still supports a review-only candidate
        # for a noncanonical derived model, protecting the existing guard.
        other["repo_hints"]=[native["repo"]]
        self.assertEqual(f.classify_artifact_from_inventory(cfg,native,[other])["state"],
                         "candidate")

    def test_derived_exl3_subset_cannot_be_partial_or_complete_native(self):
        """Regression: an Engram subset must never be rsynced into EXL3."""
        cfg=cfg2()
        base="/home/zbig/gguf/hf/"
        exl3_path=base+"Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw"
        native_path=base+"deepseek-ai/DeepSeek-V4.1-Flash"
        shard_a="model-00047-of-00048.safetensors"
        shard_b="model-00048-of-00048.safetensors"
        files=["config.json","model.safetensors.index.json",shard_a,shard_b]
        exl3={
            "kind":"huggingface","format":"safetensors-directory",
            "repo":"Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw",
            "revision":"","mode":"full","expected_shards":39,
            "legacy_names":["DeepSeek-V4.1-Flash-EXL3-2.9bpw"],"recipes":["coop"],
        }
        native={
            "kind":"huggingface","format":"safetensors-directory",
            "repo":"deepseek-ai/DeepSeek-V4.1-Flash", "revision":"",
            "mode":"subset","files":files,
            "legacy_names":["DeepSeek-V4.1-Flash"],"recipes":["coop"],
        }
        def entry(path, names, shards, *, index_expected=0, index_present=0):
            return {"path":path,"type":"directory","basename":path.rsplit('/',1)[-1],
                "files":names,"config":{},"repo_hints":["deepseek-ai/DeepSeek-V4.1-Flash"],
                "hf_commits":[],"has_config":True,"has_index":True,
                "index_expected":index_expected,"index_present":index_present,
                "safetensors":shards,"model_shards":shards,"ggufs":0,"weights":shards,
                "bytes":1234}
        # Pre-accident: EXL3 contains just 2/4 shared subset metadata files.
        exl3_clean=entry(exl3_path,files[:2],39,index_expected=39,index_present=39)
        source_native=entry(native_path,files,48,index_expected=48,index_present=48)
        plan,_=f.build_reconcile_plan(cfg,[exl3,native],{1:[exl3_clean,source_native],2:[exl3_clean]})
        self.assertEqual(plan[1]["probes"][2]["state"],"missing")
        self.assertEqual(plan[1]["probes"][2]["path"],native_path)
        self.assertEqual(plan[1]["source"],1)
        # After the accidental copy, native shards/index are also in the EXL3 dir.
        exl3_mixed=entry(exl3_path,files,87,index_expected=48,index_present=48)
        plan,_=f.build_reconcile_plan(cfg,[exl3,native],{1:[exl3_clean,source_native],2:[exl3_mixed]})
        self.assertEqual(plan[1]["probes"][2]["state"],"missing")
        self.assertEqual(plan[1]["probes"][2]["path"],native_path)
        # Even with only the native subset requested, this mismatched directory
        # is a review-only candidate, never an authoritative source/destination.
        solo=f.classify_artifact_from_inventory(cfg,native,[exl3_mixed])
        self.assertEqual(solo["state"],"candidate")

    def test_canonical_inventory_wins_over_embedded_repository_hint(self):
        cfg=cfg2()
        repo="deepseek-ai/DeepSeek-V4.1-Flash"
        canonical=f.artifact_canonical_path(cfg,{"repo":repo})
        foreign="/home/zbig/gguf/hf/Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw"
        def record(path, n):
            return {"path":path,"type":"directory","basename":path.rsplit('/',1)[-1],
                "files":["config.json"],"config":{},"repo_hints":[repo],
                "hf_commits":[],"has_config":True,"has_index":False,
                "index_expected":0,"index_present":0,"safetensors":n,
                "model_shards":n,"ggufs":0,"weights":n,"bytes":n}
        art={"repo":repo,"revision":"","mode":"full","expected_shards":48}
        probe=f.classify_artifact_from_inventory(cfg,art,[record(foreign,48),record(canonical,26)])
        self.assertEqual(probe["state"],"partial")
        self.assertEqual(probe["path"],canonical)

    def test_job20_copies_to_canonical_not_partial_probe_path(self):
        cfg=cfg2()
        artifact={"kind":"huggingface","repo":"org/native","revision":"",
                  "mode":"subset","files":["model.safetensors.index.json"],"recipes":["r"]}
        correct=f.artifact_canonical_path(cfg,artifact)
        wrong="/home/zbig/gguf/hf/org/other-model"
        plan=[{"artifact":artifact,"source":1,"probes":{
            1:{"state":"complete","path":correct},
            2:{"state":"partial","path":wrong}}}]
        # Deliberately patch build_reconcile_plan to emulate an outdated inventory
        # probe: the command must still refuse to use the partial path for writing.
        with mock.patch.object(f,"load_cfg",return_value=(cfg,None)), \
             mock.patch.object(f,"require_storage_ready"), \
             mock.patch.object(f,"selected_recipe_records",return_value=[(None,{"id":"r"})]), \
             mock.patch.object(f,"ensure_recipe_sources"), \
             mock.patch.object(f,"merge_recipe_artifacts",return_value=[artifact]), \
             mock.patch.object(f,"build_reconcile_plan",return_value=(plan,{1:[],2:[]})), \
             mock.patch.object(f,"print_reconcile_plan"), \
             mock.patch.object(f,"_ensure_reconcile_tools"), \
             mock.patch.object(f,"_ensure_canonical_alias"), \
             mock.patch.object(f,"_sync_directory_over_fabric") as sync, \
             mock.patch.object(f,"probe_artifact_on_node",return_value={"state":"complete","path":correct}):
            f.cmd_model_reconcile(mock.Mock(cluster="dgx-c1",recipe="r",apply=True,
                                           download_missing=False,seed_node=None))
        self.assertEqual(sync.call_args.args[4],correct)
        self.assertEqual(sync.call_args.kwargs["include_files"],artifact["files"])

    def test_subset_sync_replicates_only_declared_artifact_files(self):
        cfg=cfg2()
        files=["config.json","model.safetensors.index.json",
               "model-00047-of-00048.safetensors","model-00048-of-00048.safetensors"]
        with mock.patch.object(f,"ssh",return_value=mock.Mock(returncode=0)) as ssh_mock:
            f._sync_directory_over_fabric(cfg,1,"/models/full",2,"/models/subset",
                                          include_files=files)
        cmd=ssh_mock.call_args.args[2]
        for name in files:
            self.assertIn("--include=/"+name,cmd)
        self.assertIn("--exclude=",cmd)
        self.assertIn("/models/subset/",cmd)
        self.assertNotIn("--delete",cmd)

    def test_subset_sync_rejects_unsafe_file_names(self):
        with self.assertRaises(SystemExit):
            f._sync_directory_over_fabric(cfg2(),1,"/source",2,"/dest",
                                          include_files=["../data"])

    def test_fabric_sync_refuses_symlink_destination(self):
        cfg=cfg2()
        with mock.patch.object(f,"ssh",return_value=mock.Mock(returncode=0)) as ssh_mock:
            f._sync_directory_over_fabric(cfg,1,"/models/source",2,"/models/dest")
        cmd=ssh_mock.call_args.args[2]
        self.assertIn("test ! -L",cmd)
        self.assertIn("Refusing rsync into symlinked model destination",cmd)

    def test_hf_download_command_retries_resumable_failures(self):
        c=cfg2()
        artifact={"kind":"huggingface","repo":"org/model","revision":"","mode":"full","files":[]}
        result=mock.Mock(returncode=0)
        with mock.patch.dict(os.environ,{},clear=True), \
             mock.patch.object(f,"ssh",return_value=result) as ssh_mock:
            f._download_artifact_on_node(c,1,artifact,"/home/zbig/gguf/hf/org/model")
        command=ssh_mock.call_args.args[2]
        self.assertIn("for attempt in 1 2 3",command)
        self.assertIn("retrying resumable download",command)
        self.assertEqual(command.count('"$HFV/bin/hf" download'),1)


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


    def test_runtime_reconcile_parser_accepts_job25_pipeline(self):
        args=f.build_parser().parse_args([
            "runtime-reconcile","--cluster","dgx-c1","--topology","tp2",
            "--recipe","v41-exl3-vision-coop","--fetch","--apply","--gate"
        ])
        self.assertEqual(args.recipe,["v41-exl3-vision-coop"])
        self.assertTrue(args.fetch and args.apply and args.gate)

    def test_runtime_artifact_paths_are_content_addressed(self):
        c=cfg2()
        artifact={
            "sha256":"9a9c44f0e423e3cfe595f195925e520af8e1b5bc56814fcaefccda87a4e983ae",
            "stage":{
                "host_template":"{home}/.cache/vllm-dsv41-flash-exl3/coop/{sha12}",
                "container_template":"/root/.cache/vllm/coop/{sha12}",
            },
        }
        host,container,host_mount,container_mount=f._runtime_artifact_dirs(c,artifact)
        self.assertEqual(host,"/home/zbig/.cache/vllm-dsv41-flash-exl3/coop/9a9c44f0e423")
        self.assertEqual(container,"/root/.cache/vllm/coop/9a9c44f0e423")
        self.assertEqual(host_mount,"/home/zbig/.cache/vllm-dsv41-flash-exl3")
        self.assertEqual(container_mount,"/root/.cache/vllm")

    def test_runtime_activation_env_injects_overlay_and_pinned_image(self):
        c=cfg2()
        artifact={
            "sha256":"9a9c44f0e423e3cfe595f195925e520af8e1b5bc56814fcaefccda87a4e983ae",
            "stage":{
                "host_template":"{home}/.cache/vllm-dsv41-flash-exl3/coop/{sha12}",
                "container_template":"/root/.cache/vllm/coop/{sha12}",
                "overlay_name":"exl3-cooperative.py",
            },
            "activation":{
                "overlay_env":"DGX_COOP_OVERLAY_HOST",
                "env":{"DGX_COOP_TEMP_ROWS_FUSED":"8","DGX_COOP_IMAGE":"image@sha256:abc"},
            },
        }
        env=f._runtime_activation_env(c,artifact)
        self.assertEqual(env["DGX_COOP_TEMP_ROWS_FUSED"],"8")
        self.assertEqual(env["DGX_COOP_IMAGE"],"image@sha256:abc")
        self.assertTrue(env["DGX_COOP_OVERLAY_HOST"].endswith("/9a9c44f0e423/exl3-cooperative.py"))

    def test_coop_recipe_start_requires_all_rank_gate_state(self):
        c=cfg2(); t={"name":"tp2","tp":2,"nodes":[1,2]}
        recipe={"id":"v41-exl3-vision-coop","runtime_artifact_provider":"recipes/runtime_artifacts/v41_exl3_coop.py"}
        manifest={"runtime_artifacts":[{
            "id":"coop","kind":"native-library",
            "sha256":"9a9c44f0e423e3cfe595f195925e520af8e1b5bc56814fcaefccda87a4e983ae"
        }]}
        with mock.patch.object(f,"_runtime_artifact_provider_manifest",return_value=manifest), \
             mock.patch.object(f,"_load_runtime_state",return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                f._recipe_runtime_artifacts_ready(c,"dgx-c1",t,recipe,required=True)
        self.assertIn("25-runtime-reconcile.sh",str(ctx.exception))

    def test_runtime_gate_command_uses_pinned_image_and_54_case_contract(self):
        artifact={
            "id":"coop",
            "sha256":"9a9c44f0e423e3cfe595f195925e520af8e1b5bc56814fcaefccda87a4e983ae",
            "runtime_py":{"sha256":"20c1bbf2663f61843c6c46de803bb134243c62799e3762ca3f5afe28c533491a"},
            "stage":{"overlay_name":"exl3-cooperative.py"},
            "image":{
                "reference":"image@sha256:digest",
                "digest":"sha256:digest",
                "legacy_config_id":"sha256:imageid",
            },
            "gate":{"checks":54,"conflicting_containers":["dsv41-exl3-head"]},
        }
        cmd=f._runtime_gate_command(artifact,"/cache/coop","/cache","/root/.cache/vllm")
        self.assertIn("image@sha256:digest",cmd)
        self.assertIn("sha256:digest",cmd)
        self.assertIn("sha256:imageid",cmd)
        self.assertIn("unexpected image identity",cmd)
        self.assertIn("test_cuda_integration.py",cmd)
        self.assertIn("dsv41-exl3-head",cmd)
        self.assertIn(" 54",cmd)



if __name__ == "__main__": unittest.main()
