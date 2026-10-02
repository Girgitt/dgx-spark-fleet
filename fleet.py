#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "cluster.toml"
STATE = ROOT / ".state"
PROFILES = ROOT / "profiles"


def eprint(*a):
    print(*a, file=sys.stderr)


def run(cmd, *, cwd=None, check=True, capture=False, input_text=None):
    if isinstance(cmd, str):
        printable = cmd
        argv = ["bash", "-lc", cmd]
    else:
        printable = " ".join(shlex.quote(str(x)) for x in cmd)
        argv = [str(x) for x in cmd]
    print(f"+ {printable}")
    return subprocess.run(
        argv,
        cwd=cwd,
        check=check,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def load_toml(path: Path):
    with path.open("rb") as f:
        return tomllib.load(f)


def load_cluster(path: Path):
    if not path.exists():
        raise SystemExit(
            f"Missing {path}. Run './fleet.py init-config', edit the file, then run network discover."
        )
    cfg = load_toml(path)
    if cfg.get("version") != 1:
        raise SystemExit("Unsupported cluster config version")
    return cfg


def profile_path(name: str) -> Path:
    p = PROFILES / f"{name}.toml"
    if not p.exists():
        raise SystemExit(f"Unknown profile: {name}")
    return p


def load_profile(name: str):
    return load_toml(profile_path(name))


def list_profiles():
    out = []
    for p in sorted(PROFILES.glob("*.toml")):
        d = load_toml(p)
        out.append((d["id"], d.get("title", "")))
    return out


def topo_nodes(cluster, profile):
    topo = profile["topology"]
    names = cluster.get("topologies", {}).get(topo, {}).get("nodes")
    if not names:
        raise SystemExit(f"Topology {topo!r} missing in cluster config")
    nodes = []
    for name in names:
        node = cluster.get("nodes", {}).get(name)
        if not node:
            raise SystemExit(f"Node {name!r} missing in cluster config")
        n = dict(node)
        n["name"] = name
        nodes.append(n)
    return nodes


def all_nodes(cluster):
    out = []
    for name, node in cluster.get("nodes", {}).items():
        n = dict(node)
        n["name"] = name
        out.append(n)
    return out


def global_user(cluster, node):
    return node.get("ssh_user") or cluster["cluster"].get("ssh_user") or os.environ.get("USER", "nvidia")


def ssh_identity(cluster, node):
    v = node.get("ssh_identity") or cluster["cluster"].get("ssh_identity")
    return os.path.expanduser(v) if v else None


def node_target(cluster, node):
    return f"{global_user(cluster, node)}@{node.get('ssh_host', node['name'])}"


def node_run(cluster, node, command: str, *, check=True, capture=False, input_text=None):
    if node.get("local", False):
        return run(command, check=check, capture=capture, input_text=input_text)
    argv = ["ssh", "-o", "BatchMode=yes"]
    ident = ssh_identity(cluster, node)
    if ident:
        argv += ["-i", ident]
    argv += [node_target(cluster, node), "bash", "-lc", shlex.quote(command)]
    return run(argv, check=check, capture=capture, input_text=input_text)


def copy_to_node(cluster, node, src: Path, dst: str):
    if node.get("local", False):
        run(["install", "-m", "0644", str(src), dst])
        return
    argv = ["scp", "-q"]
    ident = ssh_identity(cluster, node)
    if ident:
        argv += ["-i", ident]
    argv += [str(src), f"{node_target(cluster, node)}:{dst}"]
    run(argv)


def primary_roce(node):
    planes = node.get("roce") or []
    if not planes:
        raise SystemExit(f"Node {node['name']} has no RoCE interfaces configured")
    p = planes[0]
    if "CHANGE_ME" in (p.get("ifname", ""), p.get("ibdev", "")):
        raise SystemExit(f"Node {node['name']} still has CHANGE_ME RoCE values")
    return p


def ip_only(cidr: str) -> str:
    return str(ipaddress.ip_interface(cidr).ip)


def net_only(cidr: str) -> str:
    return str(ipaddress.ip_interface(cidr).network)


def render_netplan(cluster, node):
    mtu = int(cluster["cluster"].get("roce_mtu", 0) or 0)
    planes = node.get("roce") or []
    if not planes:
        raise SystemExit(f"No RoCE planes for {node['name']}")
    lines = ["network:", "  version: 2", "  ethernets:"]
    for p in planes:
        ifname = p.get("ifname", "")
        if not ifname or ifname == "CHANGE_ME":
            raise SystemExit(f"Set RoCE ifname for {node['name']} before applying network")
        addr = p["address"]
        lines += [
            f"    {ifname}:",
            "      dhcp4: false",
            "      dhcp6: false",
            "      optional: true",
            "      addresses:",
            f"        - {addr}",
        ]
        if mtu:
            lines.append(f"      mtu: {mtu}")
    return "\n".join(lines) + "\n"


def apply_netplan(cluster, node, text):
    payload = base64.b64encode(text.encode()).decode()
    cmd = (
        "set -euo pipefail; "
        f"echo {shlex.quote(payload)} | base64 -d | sudo -n tee /etc/netplan/40-dgx-spark-roce.yaml >/dev/null; "
        "sudo -n chmod 600 /etc/netplan/40-dgx-spark-roce.yaml; "
        "sudo -n netplan generate; sudo -n netplan apply"
    )
    node_run(cluster, node, cmd)


def shell_assign(key, value):
    return f"{key}={shlex.quote(str(value))}"


def merge_env(example: Path, target: Path, overrides: dict[str, str]):
    text = example.read_text()
    for key, value in overrides.items():
        line = shell_assign(key, value)
        pattern = re.compile(rf"(?m)^[ \t]*{re.escape(key)}=.*$")
        if pattern.search(text):
            text = pattern.sub(line, text, count=1)
        else:
            text += f"\n# fleet override\n{line}\n"
    target.write_text(text)
    os.chmod(target, 0o600)


def ensure_source(profile):
    src = ROOT / profile["source"]
    if not src.exists() or not any(src.iterdir()):
        raise SystemExit(
            f"Source submodule is not initialized: {src}\nRun: ./fleet.py sources init"
        )
    return src


def common_management_iface(cluster):
    return cluster["cluster"].get("management_interface", "")


def configure_mia_exl3(cluster, profile):
    src = ensure_source(profile)
    nodes = topo_nodes(cluster, profile)
    if len(nodes) != 2:
        raise SystemExit("mia_exl3 adapter requires two nodes")
    head, worker = nodes
    hp, wp = primary_roce(head), primary_roce(worker)
    head_ip, worker_ip = ip_only(hp["address"]), ip_only(wp["address"])
    example = src / profile["env_example"]
    target = src / profile["env_file"]
    overrides = {
        "HEAD_IP": head_ip,
        "WORKER_IP": worker_ip,
        "WORKER_USER": global_user(cluster, worker),
        "WORKER_SSH": f"{global_user(cluster, worker)}@{worker_ip}",
        "HEAD_CX7_IF": hp["ifname"],
        "WORKER_CX7_IF": wp["ifname"],
        "HEAD_CX7_IB": hp["ibdev"],
        "WORKER_CX7_IB": wp["ibdev"],
        "NFS_SERVER_IP": head_ip,
        "NFS_CLIENTS": f"{worker_ip},{net_only(hp['address'])}",
        "PORT": profile.get("port", 8888),
        "TP": 2,
        "NNODES": 2,
        "SERVED_MODEL_NAME": profile.get("served_model", "DeepSeek-v4.1-Flash-EXL3"),
    }
    overrides.update({k: str(v) for k, v in profile.get("env", {}).items()})
    merge_env(example, target, overrides)
    print(f"Wrote {target.relative_to(ROOT)}")
    return target


def configure_mia_sglang_tp4(cluster, profile):
    src = ensure_source(profile)
    nodes = topo_nodes(cluster, profile)
    if len(nodes) != 4:
        raise SystemExit("mia_sglang_tp4 adapter requires four nodes")
    head, *workers = nodes
    hplane = primary_roce(head)
    # The upstream TP4 recipe assumes the same fabric interface naming on every rank.
    fabric_names = {primary_roce(n)["ifname"] for n in nodes}
    if len(fabric_names) != 1:
        raise SystemExit(
            "MiaAI TP4 recipe currently expects one common FABRIC_IFACE name on every Spark; "
            f"configured names are {sorted(fabric_names)}"
        )
    control_ips = []
    for n in nodes:
        c = n.get("control_ip")
        if not c:
            raise SystemExit(f"Set control_ip for {n['name']} for the SGLang TP4 profile")
        control_ips.append(c)
    mgmt_if = common_management_iface(cluster)
    if not mgmt_if:
        raise SystemExit("Set cluster.management_interface for SGLang TP4")
    ib_hca = ",".join(p["ibdev"] for p in head.get("roce", []))
    nfs_net = net_only(hplane["address"])
    example = src / profile["env_example"]
    target = src / profile["env_file"]
    overrides = {
        "HEAD_IP": control_ips[0],
        "WORKER_IPS": " ".join(control_ips[1:]),
        "WORKER_HOSTS": " ".join(n.get("ssh_host", n["name"]) for n in workers),
        "WORKER_USER": global_user(cluster, workers[0]),
        "SSH_IDENTITY": ssh_identity(cluster, workers[0]) or "",
        "FABRIC_IFACE": next(iter(fabric_names)),
        "GLOO_SOCKET_IFNAME": mgmt_if,
        "NCCL_SOCKET_IFNAME": mgmt_if,
        "IB_HCA": ib_hca,
        "NFS_SERVER_IPS": ip_only(hplane["address"]),
        "NFS_CLIENTS": nfs_net,
        "PORT": profile.get("port", 8888),
        "NNODES": 4,
        "TP_SIZE": 4,
    }
    overrides.update({k: str(v) for k, v in profile.get("env", {}).items()})
    merge_env(example, target, overrides)
    print(f"Wrote {target.relative_to(ROOT)}")
    return target


def replace_assignment(text, key, value):
    pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    line = shell_assign(key, value)
    if not pattern.search(text):
        raise RuntimeError(f"Could not find {key}= in upstream launcher")
    return pattern.sub(line, text, count=1)


def render_v4_launcher(cluster, profile):
    srcroot = ensure_source(profile)
    src = srcroot / profile["launcher"]
    text = src.read_text()
    nodes = topo_nodes(cluster, profile)
    head_ip = ip_only(primary_roce(nodes[0])["address"])
    for key, value in [
        ("IMAGE", profile["image"]),
        ("MASTER_ADDR", head_ip),
        ("MASTER_PORT", profile.get("master_port", 25440)),
        ("PORT", profile.get("port", 8888)),
    ]:
        text = replace_assignment(text, key, value)

    case_lines = ['case "$NODE_RANK" in']
    for rank, node in enumerate(nodes):
        p = primary_roce(node)
        headless = "" if rank == 0 else "--headless"
        case_lines.append(
            f"  {rank}) HOST_IP={shlex.quote(ip_only(p['address']))}; "
            f"HEADLESS={shlex.quote(headless)}; MODELS_HOST={shlex.quote(node.get('model_host','/var/tmp/models'))}; "
            f"FLEET_NCCL_IB_HCA={shlex.quote(p['ibdev'])}; FLEET_SOCKET_IF={shlex.quote(p['ifname'])} ;;"
        )
    case_lines += [f'  *) echo "rank must be 0..{len(nodes)-1}" >&2; exit 2 ;;', 'esac']
    case_block = "\n".join(case_lines)
    text, n = re.subn(r'case "\$NODE_RANK" in\n.*?\nesac', case_block, text, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError("Could not replace upstream rank case block")

    text = re.sub(r'-e NCCL_IB_HCA=[^ \\\n]+', '-e NCCL_IB_HCA="$FLEET_NCCL_IB_HCA"', text)
    text = re.sub(r'-e NCCL_SOCKET_IFNAME=[^ ]+ -e GLOO_SOCKET_IFNAME=[^ ]+ -e TP_SOCKET_IFNAME=[^ \\\n]+',
                  '-e NCCL_SOCKET_IFNAME="$FLEET_SOCKET_IF" -e GLOO_SOCKET_IFNAME="$FLEET_SOCKET_IF" -e TP_SOCKET_IFNAME="$FLEET_SOCKET_IF"', text)
    text = text.replace("-e NCCL_IB_MERGE_NICS=0", "-e NCCL_IB_MERGE_NICS=0")
    text = "# GENERATED by dgx-spark-deepseek-fleet; do not edit.\n" + text
    outdir = STATE / "rendered" / profile["id"]
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / "launcher.sh"
    out.write_text(text)
    os.chmod(out, 0o755)
    print(f"Rendered {out.relative_to(ROOT)}")
    return out


def configure_profile(cluster, profile):
    adapter = profile["adapter"]
    if adapter == "mia_exl3":
        return configure_mia_exl3(cluster, profile)
    if adapter == "mia_sglang_tp4":
        return configure_mia_sglang_tp4(cluster, profile)
    if adapter == "v4_launcher":
        return render_v4_launcher(cluster, profile)
    raise SystemExit(f"Unknown adapter {adapter}")


def v4_stage_files(cluster, profile):
    src = ensure_source(profile)
    nodes = topo_nodes(cluster, profile)
    image = profile["image"]
    # Build the four vision-port files against the pinned image on the head.
    build = src / "vision-exp" / "build-ds4v-files.sh"
    if not build.exists():
        raise SystemExit(f"Missing upstream helper {build}")
    head = nodes[0]
    if not head.get("local", False):
        raise SystemExit("v4 preparation currently expects the head to be the local controller")
    probe = run(["docker", "image", "inspect", image], check=False, capture=True)
    if probe.returncode:
        raise SystemExit(
            f"Required upstream runtime image is not present: {image}\n"
            "Build/restore it using the pinned upstream V4 Vision recipe first; this orchestrator deliberately does not invent a replacement image."
        )
    run([str(build), image, "/var/tmp"], cwd=src)
    mapping = {
        "patch3-scheduler.py": src / "recipe/overlay/vllm/v1/core/sched/scheduler.py",
        "spec-dspark.py": src / "recipe/overlay/vllm/v1/spec_decode/dspark.py",
    }
    for dst, source in mapping.items():
        if not source.exists():
            raise SystemExit(f"Missing upstream source {source}")
        shutil.copy2(source, Path("/var/tmp") / dst)
    required = [
        "patch3-scheduler.py", "spec-dspark.py", "ds4v_model.py", "ds4v_vision.py", "ds4v_mm.py", "ds4v_registry.py"
    ]
    for name in required:
        p = Path("/var/tmp") / name
        if not p.exists():
            raise SystemExit(f"Upstream preparation did not produce {p}")
    for node in nodes[1:]:
        for name in required:
            copy_to_node(cluster, node, Path("/var/tmp") / name, f"/var/tmp/{name}")
    print("V4 Vision port files staged on all ranks.")


def prepare_profile(cluster, profile):
    configure_profile(cluster, profile)
    src = ensure_source(profile)
    adapter = profile["adapter"]
    if adapter == "v4_launcher":
        v4_stage_files(cluster, profile)
        return
    if adapter == "mia_exl3":
        # Download-only stage is resumable; start.sh handles image/NFS/launch itself.
        run(["./download.sh"], cwd=src)
        return
    if adapter == "mia_sglang_tp4":
        for step in ("doctor", "build", "download", "share", "pack"):
            run(["./start-tp4.sh", step], cwd=src)
        return


def start_profile(cluster, profile):
    configure_profile(cluster, profile)
    src = ensure_source(profile)
    adapter = profile["adapter"]
    if adapter == "mia_exl3":
        run(["./start.sh"], cwd=src)
    elif adapter == "mia_sglang_tp4":
        run(["./start-tp4.sh", "serve"], cwd=src)
    elif adapter == "v4_launcher":
        launcher = render_v4_launcher(cluster, profile)
        nodes = topo_nodes(cluster, profile)
        remote_path = f"/var/tmp/dgx-fleet-{profile['id']}.sh"
        for n in nodes:
            copy_to_node(cluster, n, launcher, remote_path)
            node_run(cluster, n, f"chmod 755 {shlex.quote(remote_path)}")
        # worker-first as required by the upstream launcher.
        for rank in reversed(range(1, len(nodes))):
            node_run(cluster, nodes[rank], f"MODEL_DIR={shlex.quote(profile['model_dir'])} {shlex.quote(remote_path)} {rank}")
        node_run(cluster, nodes[0], f"MODEL_DIR={shlex.quote(profile['model_dir'])} {shlex.quote(remote_path)} 0")
    else:
        raise SystemExit(f"Unknown adapter {adapter}")


def stop_profile(cluster, profile):
    src = ensure_source(profile)
    adapter = profile["adapter"]
    if adapter == "mia_exl3":
        run(["./start.sh", "stop"], cwd=src, check=False)
    elif adapter == "mia_sglang_tp4":
        run(["./start-tp4.sh", "stop"], cwd=src, check=False)
    elif adapter == "v4_launcher":
        for n in topo_nodes(cluster, profile):
            node_run(cluster, n, f"docker rm -f {shlex.quote(profile['container'])} 2>/dev/null || true", check=False)


def status_profile(cluster, profile):
    src = ensure_source(profile)
    adapter = profile["adapter"]
    if adapter == "mia_exl3":
        run(["./start.sh", "status"], cwd=src, check=False)
    elif adapter == "mia_sglang_tp4":
        run(["./start-tp4.sh", "status"], cwd=src, check=False)
    elif adapter == "v4_launcher":
        for n in topo_nodes(cluster, profile):
            print(f"\n== {n['name']} ==")
            node_run(cluster, n, f"docker ps --filter name=^{profile['container']}$ --format '{{{{.Names}}}} {{{{.Status}}}}'", check=False)


def health_url(cluster, profile):
    nodes = topo_nodes(cluster, profile)
    head = nodes[0]
    host = "127.0.0.1" if head.get("local", False) else head.get("control_ip") or ip_only(primary_roce(head)["address"])
    return f"http://{host}:{profile.get('port',8888)}"


def wait_health(cluster, profile, attempts=90, delay=5):
    url = health_url(cluster, profile) + "/health"
    print(f"Health: {url}")
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if 200 <= r.status < 300:
                    print("health OK")
                    return True
        except Exception:
            pass
        time.sleep(delay)
    eprint("Health endpoint did not become ready within the polling window; inspect upstream logs.")
    return False


def smoke_profile(cluster, profile, api_key):
    base = health_url(cluster, profile) + "/v1"
    cmd = [sys.executable, str(ROOT / "scripts/smoke-openai.py"), "--base-url", base, "--model", profile["served_model"]]
    if api_key:
        cmd += ["--api-key", api_key]
    run(cmd)


def active_profile_name():
    p = STATE / "active-profile"
    return p.read_text().strip() if p.exists() else None


def set_active(name):
    STATE.mkdir(exist_ok=True)
    (STATE / "active-profile").write_text(name + "\n")


def clear_active(name=None):
    p = STATE / "active-profile"
    if p.exists() and (name is None or p.read_text().strip() == name):
        p.unlink()


def cmd_init_config(args):
    dst = Path(args.config)
    if dst.exists() and not args.force:
        raise SystemExit(f"{dst} already exists; use --force to replace it")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "config/cluster.example.toml", dst)
    print(f"Created {dst}. Edit SSH/control addresses and RoCE interface names before applying network.")


def cmd_sources(args):
    if args.action == "init":
        run(["git", "submodule", "update", "--init", "--recursive"], cwd=ROOT)
    elif args.action == "status":
        run(["git", "submodule", "status"], cwd=ROOT, check=False)
    elif args.action == "update":
        run(["git", "submodule", "update", "--remote", "--merge"], cwd=ROOT)
        print("Review upstream changes and commit the new gitlinks deliberately; profiles are pinned by this repository commit.")


def cmd_network(args):
    cluster = load_cluster(Path(args.config))
    nodes = all_nodes(cluster)
    if args.action == "discover":
        cmd = "hostname; echo '--- ibdev2netdev'; ibdev2netdev || true; echo '--- rdma'; rdma link || true; echo '--- addresses'; ip -br addr"
        for n in nodes:
            print(f"\n===== {n['name']} =====")
            node_run(cluster, n, cmd, check=False)
    elif args.action == "render":
        out = STATE / "netplan"
        out.mkdir(parents=True, exist_ok=True)
        for n in nodes:
            text = render_netplan(cluster, n)
            p = out / f"{n['name']}.yaml"
            p.write_text(text)
            print(f"\n# {p.relative_to(ROOT)}\n{text}")
    elif args.action == "apply":
        if not args.yes:
            raise SystemExit("Refusing to change networking without --yes. Run 'network render' first.")
        for n in nodes:
            print(f"Applying RoCE netplan on {n['name']} (management NIC is not touched)...")
            apply_netplan(cluster, n, render_netplan(cluster, n))
    elif args.action == "verify":
        for n in nodes:
            print(f"\n===== {n['name']} RDMA =====")
            node_run(cluster, n, "ibdev2netdev; rdma link; ibv_devinfo -l", check=False)
        maxplanes = max((len(n.get("roce") or []) for n in nodes), default=0)
        for plane_idx in range(maxplanes):
            peers = [(n, n.get("roce", [])[plane_idx]) for n in nodes if len(n.get("roce") or []) > plane_idx]
            for src, sp in peers:
                for dst, dp in peers:
                    if src["name"] == dst["name"]:
                        continue
                    dip = ip_only(dp["address"])
                    print(f"{src['name']} plane{plane_idx} -> {dst['name']} {dip}")
                    node_run(cluster, src, f"ping -I {shlex.quote(sp['ifname'])} -c 1 -W 2 {shlex.quote(dip)}", check=False)


def cmd_bootstrap(args):
    cluster = load_cluster(Path(args.config))
    script = (ROOT / "scripts/bootstrap-node.sh").read_text()
    for n in all_nodes(cluster):
        print(f"\n===== {n['name']} =====")
        if n.get("local", False):
            run([str(ROOT / "scripts/bootstrap-node.sh")], check=False)
        else:
            argv = ["ssh", "-o", "BatchMode=yes"]
            ident = ssh_identity(cluster, n)
            if ident:
                argv += ["-i", ident]
            argv += [node_target(cluster, n), "bash", "-s"]
            run(argv, check=False, input_text=script)


def cmd_profile(args):
    if args.action == "list":
        for pid, title in list_profiles():
            marker = "*" if active_profile_name() == pid else " "
            print(f"{marker} {pid:30} {title}")
        return
    profile = load_profile(args.name)
    cluster = load_cluster(Path(args.config))
    if args.action == "show":
        print(json.dumps(profile, indent=2))
    elif args.action == "configure":
        configure_profile(cluster, profile)
    elif args.action == "prepare":
        prepare_profile(cluster, profile)
    elif args.action == "start":
        start_profile(cluster, profile)
        set_active(profile["id"])
        if not args.no_health:
            wait_health(cluster, profile)
    elif args.action == "stop":
        stop_profile(cluster, profile)
        clear_active(profile["id"])
    elif args.action == "status":
        status_profile(cluster, profile)
    elif args.action == "smoke":
        smoke_profile(cluster, profile, args.api_key)


def cmd_switch(args):
    cluster = load_cluster(Path(args.config))
    target = load_profile(args.name)
    active = active_profile_name()
    if active and active != target["id"]:
        print(f"Stopping active profile {active}...")
        stop_profile(cluster, load_profile(active))
        clear_active(active)
    configure_profile(cluster, target)
    if args.prepare:
        prepare_profile(cluster, target)
    start_profile(cluster, target)
    set_active(target["id"])
    if not args.no_health:
        wait_health(cluster, target)


def build_parser():
    p = argparse.ArgumentParser(description="DGX Spark DeepSeek deployment/profile controller")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("init-config")
    q.add_argument("--force", action="store_true")
    q.set_defaults(func=cmd_init_config)

    q = sub.add_parser("sources")
    q.add_argument("action", choices=["init", "status", "update"])
    q.set_defaults(func=cmd_sources)

    q = sub.add_parser("bootstrap")
    q.set_defaults(func=cmd_bootstrap)

    q = sub.add_parser("network")
    q.add_argument("action", choices=["discover", "render", "apply", "verify"])
    q.add_argument("--yes", action="store_true")
    q.set_defaults(func=cmd_network)

    q = sub.add_parser("profile")
    q.add_argument("action", choices=["list", "show", "configure", "prepare", "start", "stop", "status", "smoke"])
    q.add_argument("name", nargs="?")
    q.add_argument("--no-health", action="store_true")
    q.add_argument("--api-key", default="")
    q.set_defaults(func=cmd_profile)

    q = sub.add_parser("switch")
    q.add_argument("name")
    q.add_argument("--prepare", action="store_true", help="run the profile's heavy first-time preparation before starting")
    q.add_argument("--no-health", action="store_true")
    q.set_defaults(func=cmd_switch)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd == "profile" and args.action != "list" and not args.name:
        parser.error("profile action requires NAME")
    args.func(args)


if __name__ == "__main__":
    main()
