#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import ipaddress
import json
import os
import posixpath
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parent
CLUSTERS = ROOT / "config" / "clusters"
STATE = ROOT / ".state" / "simple"
RECIPES = ROOT / "recipes"


def run(argv, *, check=True, capture=False, input_text=None):
    if isinstance(argv, str):
        printable = argv
        cmd = ["bash", "-lc", argv]
    else:
        cmd = [str(x) for x in argv]
        printable = " ".join(shlex.quote(x) for x in cmd)
    print(f"+ {printable}")
    return subprocess.run(
        cmd,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=check,
    )


def safe_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise SystemExit(f"Invalid name: {value!r}")
    return value


def cfg_path(cluster_id: str) -> Path:
    return CLUSTERS / f"{safe_name(cluster_id)}.toml"


def load_cfg(cluster_id: str):
    p = cfg_path(cluster_id)
    if not p.exists():
        raise SystemExit(f"Missing {p}. Run: ./fleetctl.py init {cluster_id} ...")
    with p.open("rb") as f:
        cfg = tomllib.load(f)
    if cfg.get("version") != 2:
        raise SystemExit(f"Unsupported simple cluster schema in {p}; expected version=2")
    if cfg.get("cluster", {}).get("id") != cluster_id:
        raise SystemExit(f"Cluster id mismatch in {p}")
    return cfg, p


def parse_node_arg(item: str):
    if "=" not in item:
        raise argparse.ArgumentTypeError("node must be INDEX=IP_OR_HOST")
    a, b = item.split("=", 1)
    try:
        idx = int(a)
    except ValueError:
        raise argparse.ArgumentTypeError("node index must be an integer")
    if idx < 1:
        raise argparse.ArgumentTypeError("node index must be >=1")
    if not b:
        raise argparse.ArgumentTypeError("node address cannot be empty")
    return idx, b


def ordered_nodes(cfg):
    return sorted(cfg.get("nodes", []), key=lambda n: int(n["index"]))


def node(cfg, idx: int):
    for n in ordered_nodes(cfg):
        if int(n["index"]) == idx:
            return n
    raise SystemExit(f"Node {idx} not found")


def ssh_user(cfg):
    return cfg["cluster"].get("ssh_user") or os.environ.get("USER", "nvidia")


def configured_model_root(cfg):
    root = str(cfg.get("cluster", {}).get("model_root", "")).strip()
    if not root:
        raise SystemExit("Missing cluster.model_root")
    if not root.startswith("/"):
        raise SystemExit(f"cluster.model_root must be an absolute path: {root!r}")
    root = posixpath.normpath(root)
    if root == "/":
        raise SystemExit("cluster.model_root must not be /")
    return root


def checked_model_path(cfg, path: str):
    root = configured_model_root(cfg)
    if not path.startswith("/"):
        raise SystemExit(f"Model path must be absolute: {path!r}")
    candidate = posixpath.normpath(path)
    if candidate != root and not candidate.startswith(root.rstrip("/") + "/"):
        raise SystemExit(f"Model path must be inside configured model_root {root}: {path}")
    return candidate


def storage_marker_expected(cfg, cluster_id: str, idx: int):
    return {
        "version": 1,
        "cluster_id": cluster_id,
        "node_index": int(idx),
        "ssh_user": ssh_user(cfg),
        "model_root": configured_model_root(cfg),
    }


def storage_marker_status(cfg, cluster_id: str, idx: int):
    marker = "/etc/dgx-spark-fleet/node.json"
    r = ssh(cfg, idx, f"cat {shlex.quote(marker)}", capture=True, check=False)
    if r.returncode:
        return False, f"missing {marker}"
    try:
        actual = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False, f"invalid JSON in {marker}"
    expected = storage_marker_expected(cfg, cluster_id, idx)
    if actual != expected:
        return False, f"stale bootstrap marker: expected {expected}, got {actual}"
    root = configured_model_root(cfg)
    r = ssh(cfg, idx, f"test -d {shlex.quote(root)} && test -w {shlex.quote(root)}", check=False)
    if r.returncode:
        return False, f"model_root is missing or not writable by {ssh_user(cfg)}: {root}"
    return True, "ok"


def require_storage_ready(cfg, cluster_id: str, indexes):
    failures = []
    for idx in sorted(set(int(x) for x in indexes)):
        ok, why = storage_marker_status(cfg, cluster_id, idx)
        if not ok:
            failures.append((idx, why))
    if failures:
        for idx, why in failures:
            print(f"FAIL node{idx}: {why}", file=sys.stderr)
        raise SystemExit(
            f"Storage bootstrap is missing or stale. Run: ./scripts/02-storage-bootstrap.sh {cluster_id}"
        )


def mng_name(cfg, idx):
    return f"mng-node{idx}.{cfg['cluster']['id']}"


def con_name(cfg, idx, rail=1):
    prefix = "con" if rail == 1 else f"con{rail}"
    return f"{prefix}-node{idx}.{cfg['cluster']['id']}"


def _nth_host(cidr: str, index: int, base: int):
    net = ipaddress.ip_network(cidr, strict=False)
    hostnum = base + index - 1
    addr = net.network_address + hostnum
    if addr not in net or addr == net.network_address or addr == net.broadcast_address:
        raise SystemExit(f"Address calculation escapes {cidr}: host offset {hostnum}")
    return str(addr)


def fabric_ip(cfg, idx, rail=1):
    key = "fabric_primary" if rail == 1 else "fabric_secondary"
    cidr = cfg["cluster"].get(key)
    if not cidr:
        raise SystemExit(f"Missing cluster.{key}")
    return _nth_host(cidr, idx, int(cfg["cluster"].get("fabric_host_base", 10)))


def is_local_address(addr: str) -> bool:
    try:
        wanted = resolve_management_ipv4(addr)
        r=subprocess.run(["ip","-o","-4","addr","show"],text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=False)
    except (FileNotFoundError, SystemExit):
        return False
    return any(part.split("/",1)[0] == wanted for line in r.stdout.splitlines() for part in line.split() if "/" in part)

def ssh(cfg, idx: int, remote: str, *, capture=False, check=True, via="management", input_text=None):
    n=node(cfg,idx)
    if via=="management" and is_local_address(n["management"]):
        return run(remote,capture=capture,check=check,input_text=input_text)
    user = ssh_user(cfg)
    host = n["management"] if via == "management" else con_name(cfg, idx)
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7",
        "-o", "ConnectionAttempts=1", f"{user}@{host}", "bash", "-lc", shlex.quote(remote),
    ]
    return run(argv, capture=capture, check=check, input_text=input_text)


def topology(cfg, name: str):
    t = cfg.get("topologies", {}).get(name)
    if not t:
        raise SystemExit(f"Unknown topology {name!r}; use './fleetctl.py topology list --cluster ...'")
    indexes = [int(x) for x in t.get("nodes", [])]
    tp = int(t.get("tp", len(indexes)))
    if tp != len(indexes):
        raise SystemExit(f"Topology {name!r}: tp={tp} but has {len(indexes)} nodes")
    for idx in indexes:
        node(cfg, idx)
    return {"name": name, "tp": tp, "nodes": indexes}


def active_topology_path(cluster_id):
    return STATE / cluster_id / "active-topology"


def set_active_topology(cluster_id, name):
    p = active_topology_path(cluster_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(name + "\n")


def get_active_topology(cluster_id):
    p = active_topology_path(cluster_id)
    return p.read_text().strip() if p.exists() else None


def resolve_management_ipv4(value: str) -> str:
    """Resolve an inventory management endpoint to one unambiguous IPv4 address.

    The inventory may contain either a literal IPv4 address or a bootstrap name
    such as ``spark-1``.  /etc/hosts entries must always start with an address,
    never another hostname, so management aliases are materialized from this
    resolved address during management bootstrap.
    """
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        addr = None
    if addr is not None:
        if addr.version != 4:
            raise SystemExit(f"Management endpoint must resolve to IPv4: {value!r}")
        return str(addr)
    try:
        infos = socket.getaddrinfo(value, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SystemExit(f"Cannot resolve management endpoint {value!r} to IPv4: {exc}") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise SystemExit(f"Cannot resolve management endpoint {value!r} to IPv4")
    if len(addresses) != 1:
        raise SystemExit(
            f"Management endpoint {value!r} resolves to multiple IPv4 addresses {addresses}; "
            "use a unique node-specific name or a literal IPv4 address"
        )
    return addresses[0]


def hosts_block(cfg):
    lines = [f"# BEGIN DGX-SPARK-FLEET {cfg['cluster']['id']}"]
    for n in ordered_nodes(cfg):
        idx = int(n["index"])
        address = resolve_management_ipv4(str(n["management"]))
        lines.append(f"{address} {mng_name(cfg, idx)}")
    for n in ordered_nodes(cfg):
        idx = int(n["index"])
        lines.append(f"{fabric_ip(cfg, idx, 1)} {con_name(cfg, idx, 1)}")
        lines.append(f"{fabric_ip(cfg, idx, 2)} {con_name(cfg, idx, 2)}")
    lines.append(f"# END DGX-SPARK-FLEET {cfg['cluster']['id']}")
    return "\n".join(lines) + "\n"


def bootstrap_hosts_script(cluster_id: str, block: str):
    marker = re.escape(cluster_id)
    return f'''set -euo pipefail
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
sudo -n awk '
  BEGIN {{ skip=0 }}
  $0 ~ /^# BEGIN DGX-SPARK-FLEET {marker}$/ {{ skip=1; next }}
  $0 ~ /^# END DGX-SPARK-FLEET {marker}$/ {{ skip=0; next }}
  !skip {{ print }}
' /etc/hosts > "$TMP"
cat >> "$TMP" <<'DGXHOSTS'
{block.rstrip()}
DGXHOSTS
sudo -n install -m 0644 "$TMP" /etc/hosts
'''


def cmd_init(args):
    cid = safe_name(args.cluster)
    nodes = dict(args.node)
    if len(nodes) < 2:
        raise SystemExit("At least two --node INDEX=ADDRESS values are required")
    indexes = sorted(nodes)
    if indexes != list(range(1, max(indexes) + 1)):
        raise SystemExit("Node indexes must be contiguous starting at 1")
    if len(nodes) > 9:
        raise SystemExit("This bootstrap intentionally limits a cluster to 9 nodes")
    out = cfg_path(cid)
    if out.exists() and not args.force:
        raise SystemExit(f"{out} exists; use --force to replace")
    out.parent.mkdir(parents=True, exist_ok=True)
    user = args.user or os.environ.get("USER", "nvidia")
    model_root = args.model_root or f"/home/{user}/dgx-models"
    lines = [
        "version = 2", "", "[cluster]", f'id = "{cid}"', f'ssh_user = "{user}"',
        f'fabric_primary = "{args.fabric_primary}"', f'fabric_secondary = "{args.fabric_secondary}"',
        "fabric_host_base = 10", f'model_root = "{model_root}"', "",
    ]
    for idx in indexes:
        lines += ["[[nodes]]", f"index = {idx}", f'management = "{nodes[idx]}"', ""]
    # Useful deterministic topology presets. Named groups may overlap by design.
    if len(nodes) >= 2:
        lines += ["[topologies.tp2]", "tp = 2", "nodes = [1, 2]", ""]
    if len(nodes) >= 3:
        lines += ["[topologies.tp3]", "tp = 3", "nodes = [1, 2, 3]", ""]
    if len(nodes) >= 4:
        lines += ["[topologies.tp4]", "tp = 4", "nodes = [1, 2, 3, 4]", "",
                  "[topologies.prod2]", "tp = 2", "nodes = [1, 2]", "",
                  "[topologies.lab2]", "tp = 2", "nodes = [3, 4]", ""]
    out.write_text("\n".join(lines))
    print(f"Created {out}")
    print("No secrets are stored. Next: ./scripts/01-management-bootstrap.sh", cid)


def cmd_hosts(args):
    cfg, _ = load_cfg(args.cluster)
    print(hosts_block(cfg), end="")


def _validate_unix_user(user: str):
    # Conservative Linux account-name check before embedding the configured user
    # into a sudoers rule.  The inventory is local, but bootstrap writes as root.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*[$]?", user):
        raise SystemExit(f"Unsafe/unsupported ssh_user for sudo bootstrap: {user!r}")


def _sudo_install_command(user: str):
    _validate_unix_user(user)
    rule = f"{user} ALL=(ALL:ALL) NOPASSWD: ALL\n"
    encoded = base64.b64encode(rule.encode()).decode()
    # Validate the candidate sudoers file before installing it.  The invoking
    # user's sudo password, if needed, is entered directly into sudo's TTY and
    # is never read, stored or transmitted by fleetctl.
    return (
        "set -euo pipefail; "
        "tmp=$(mktemp); trap 'rm -f \"$tmp\"' EXIT; "
        f"printf %s {shlex.quote(encoded)} | base64 -d > \"$tmp\"; "
        "chmod 0600 \"$tmp\"; "
        "sudo -v; "
        "sudo visudo -cf \"$tmp\"; "
        "sudo install -o root -g root -m 0440 \"$tmp\" /etc/sudoers.d/90-dgx-spark-fleet; "
        "sudo visudo -cf /etc/sudoers.d/90-dgx-spark-fleet; "
        "sudo -n true"
    )


def ensure_passwordless_sudo(cfg, idx: int):
    n = node(cfg, idx)
    user = ssh_user(cfg)
    raw = n["management"]

    # Fast/idempotent path used by normal Rundeck runs.
    if is_local_address(raw):
        probe = run(["sudo", "-n", "true"], check=False)
    else:
        probe = run([
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7",
            "-o", "ConnectionAttempts=1", f"{user}@{raw}",
            "sudo", "-n", "true",
        ], check=False)
    if probe.returncode == 0:
        print(f"node{idx}: passwordless sudo already configured")
        return True

    print(
        f"node{idx}: passwordless sudo is not configured for {user}; "
        "bootstrapping it now. sudo may prompt once for this node."
    )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            f"node{idx}: interactive sudo bootstrap requires a terminal. "
            "Run scripts/01-management-bootstrap.sh once from an interactive shell; "
            "subsequent Rundeck runs will be non-interactive.",
            file=sys.stderr,
        )
        return False
    cmd = _sudo_install_command(user)
    if is_local_address(raw):
        # Inherit the operator's terminal so sudo reads the password itself.
        result = run(cmd, check=False)
    else:
        # -tt deliberately allocates a TTY for the one-time sudo password.
        # No password is passed through Python, argv, environment or files.
        result = run([
            "ssh", "-tt", "-o", "ConnectTimeout=7",
            "-o", "ConnectionAttempts=1", f"{user}@{raw}",
            "bash", "-lc", shlex.quote(cmd),
        ], check=False)
    if result.returncode != 0:
        return False

    # Prove that future automation is non-interactive.
    if is_local_address(raw):
        verify = run(["sudo", "-n", "true"], check=False)
    else:
        verify = run([
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7",
            "-o", "ConnectionAttempts=1", f"{user}@{raw}",
            "sudo", "-n", "true",
        ], check=False)
    return verify.returncode == 0



def _parse_ssh_host_public_keys(text: str):
    """Return normalized ``(key_type, base64_key)`` SSH host public keys.

    Host-key comments are deliberately discarded. Only public material is
    propagated; private host keys never leave the node that owns them.
    """
    out = []
    seen = set()
    for raw in text.splitlines():
        parts = raw.strip().split()
        if len(parts) < 2:
            continue
        key_type, key = parts[0], parts[1]
        if not (key_type.startswith("ssh-") or key_type.startswith("ecdsa-")):
            continue
        if not re.fullmatch(r"[A-Za-z0-9+/=]+", key):
            continue
        item = (key_type, key)
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _read_node_host_public_keys(cfg, idx: int):
    # Read public host keys over the already-authenticated management channel.
    # This avoids ssh-keyscan/TOFU and works before fabric aliases are reachable.
    cmd = (
        "set -e; found=0; "
        "for f in /etc/ssh/ssh_host_*_key.pub; do "
        "  [ -r \"$f\" ] || continue; "
        "  awk '\''NF >= 2 {print $1, $2}'\'' \"$f\"; found=1; "
        "done; test \"$found\" = 1"
    )
    r = ssh(cfg, idx, cmd, capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"node{idx}: could not read SSH host public keys")
    keys = _parse_ssh_host_public_keys(r.stdout)
    if not keys:
        raise RuntimeError(f"node{idx}: no usable SSH host public keys found")
    return keys


def ssh_known_hosts_block(cfg, keys_by_node):
    cid = cfg["cluster"]["id"]
    lines = [f"# BEGIN DGX-SPARK-FLEET {cid} SSH-HOST-KEYS"]
    for n in ordered_nodes(cfg):
        idx = int(n["index"])
        aliases = ",".join((
            mng_name(cfg, idx), con_name(cfg, idx), con_name(cfg, idx, 2),
            resolve_management_ipv4(n["management"]), fabric_ip(cfg, idx, 1), fabric_ip(cfg, idx, 2),
        ))
        keys = keys_by_node.get(idx) or []
        if not keys:
            raise RuntimeError(f"node{idx}: missing SSH host public keys")
        for key_type, key in keys:
            lines.append(f"{aliases} {key_type} {key}")
    lines.append(f"# END DGX-SPARK-FLEET {cid} SSH-HOST-KEYS")
    return "\n".join(lines) + "\n"


def _known_hosts_install_script(cid: str, block: str):
    begin = f"# BEGIN DGX-SPARK-FLEET {cid} SSH-HOST-KEYS"
    end = f"# END DGX-SPARK-FLEET {cid} SSH-HOST-KEYS"
    payload = base64.b64encode(block.encode()).decode()
    return (
        "set -euo pipefail\n"
        "mkdir -p ~/.ssh\n"
        "chmod 700 ~/.ssh\n"
        "known=~/.ssh/known_hosts\n"
        "touch \"$known\"\n"
        "chmod 600 \"$known\"\n"
        "tmp=$(mktemp)\n"
        "trap '\''rm -f \"$tmp\"'\'' EXIT\n"
        f"awk -v begin={shlex.quote(begin)} -v end={shlex.quote(end)} '\n"
        "  $0 == begin { skip=1; next }\n"
        "  $0 == end { skip=0; next }\n"
        "  !skip { print }\n"
        "' \"$known\" > \"$tmp\"\n"
        f"printf '%s' {shlex.quote(payload)} | base64 -d >> \"$tmp\"\n"
        "install -m 0600 \"$tmp\" \"$known\"\n"
    )


def install_cluster_host_trust(cfg, cid: str):
    keys_by_node = {}
    for n in ordered_nodes(cfg):
        idx = int(n["index"])
        keys_by_node[idx] = _read_node_host_public_keys(cfg, idx)
    block = ssh_known_hosts_block(cfg, keys_by_node)
    script = _known_hosts_install_script(cid, block)
    failures = []
    for n in ordered_nodes(cfg):
        idx = int(n["index"])
        r = ssh(cfg, idx, script, check=False)
        if r.returncode:
            failures.append(idx)
    if failures:
        raise RuntimeError(f"failed to install SSH host trust on nodes: {failures}")
    print("SSH host-key trust installed for management and fabric aliases")


def _docker_socket_group_command():
    return (
        "set -euo pipefail; "
        "command -v docker >/dev/null || { echo 'docker CLI is not installed' >&2; exit 41; }; "
        "test -S /var/run/docker.sock || { echo '/var/run/docker.sock is missing; Docker daemon is not available' >&2; exit 42; }; "
        "gid=$(stat -c %g /var/run/docker.sock); "
        "group=$(getent group \"$gid\" | cut -d: -f1); "
        "test -n \"$group\" || { echo \"cannot resolve Docker socket GID $gid to a group\" >&2; exit 43; }; "
        "printf '%s\\n' \"$group\""
    )


def ensure_docker_access(cfg, idx: int):
    """Ensure the configured login user can use Docker without sudo.

    The socket's owning group is discovered rather than assuming it is named
    ``docker``.  Group membership changes are verified in a fresh login context
    so later Rundeck jobs never depend on an operator re-login.
    """
    user = ssh_user(cfg)
    n = node(cfg, idx)
    raw = n["management"]

    probe = ssh(cfg, idx, _docker_socket_group_command(), capture=True, check=False)
    if probe.returncode != 0:
        return False
    group = probe.stdout.strip()
    if not group or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*[$]?", group):
        print(f"node{idx}: unsafe/unexpected Docker socket group {group!r}", file=sys.stderr)
        return False

    membership_cmd = (
        f"id -nG {shlex.quote(user)} | tr ' ' '\\n' | grep -Fxq {shlex.quote(group)}"
    )
    member = ssh(cfg, idx, membership_cmd, check=False)
    if member.returncode != 0:
        print(f"node{idx}: adding {user} to Docker socket group {group}")
        add_cmd = (
            f"sudo -n usermod -aG {shlex.quote(group)} {shlex.quote(user)}"
        )
        added = ssh(cfg, idx, add_cmd, check=False)
        if added.returncode != 0:
            return False
    else:
        print(f"node{idx}: Docker group membership already configured ({group})")

    # A group change is not visible to an already-running login shell.  Remote
    # nodes naturally get a new login on the next ssh invocation.  For a local
    # controller node, use sudo to start a fresh process with the configured
    # user's supplementary groups, without granting Docker root privileges.
    if is_local_address(raw):
        verify_cmd = [
            "sudo", "-n", "-u", user, "-H",
            "bash", "-lc", "docker version >/dev/null",
        ]
        verify = run(verify_cmd, check=False)
    else:
        verify = run([
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7",
            "-o", "ConnectionAttempts=1", f"{user}@{raw}",
            "bash", "-lc", shlex.quote("docker version >/dev/null"),
        ], check=False)
    if verify.returncode != 0:
        print(
            f"node{idx}: Docker is installed but {user} still cannot access the daemon after group bootstrap",
            file=sys.stderr,
        )
        return False
    print(f"node{idx}: Docker access OK ({group})")
    return True


def bootstrap_management(cfg, cid):
    user=ssh_user(cfg); block=hosts_block(cfg); script=bootstrap_hosts_script(cid,block)
    failures=[]
    for n in ordered_nodes(cfg):
        idx=int(n["index"]); raw=n["management"]
        print(f"\n== node{idx}: {raw} -> {mng_name(cfg,idx)} ==")

        # First establish that the configured account itself is reachable.
        # Sudo is bootstrapped separately so its failure is diagnosed clearly.
        if is_local_address(raw):
            probe=run(["bash", "-lc", f"test \"$(id -un)\" = {shlex.quote(user)} && hostname"],check=False)
        else:
            probe=run([
                "ssh","-o","BatchMode=yes","-o","ConnectTimeout=7",
                "-o","ConnectionAttempts=1",f"{user}@{raw}",
                "bash","-lc",shlex.quote(f"test \"$(id -un)\" = {shlex.quote(user)} && hostname")
            ],check=False)
        if probe.returncode:
            failures.append((idx,"passwordless SSH failed for configured user"))
            continue

        if not ensure_passwordless_sudo(cfg, idx):
            failures.append((idx,"could not establish passwordless sudo (user must already have sudo rights)"))
            continue

        if not ensure_docker_access(cfg, idx):
            failures.append((idx,"could not establish non-root Docker access for configured user"))
            continue

        if is_local_address(raw):
            apply=run(["bash","-s"],check=False,input_text=script)
        else:
            apply=run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=7",f"{user}@{raw}","bash","-s"],check=False,input_text=script)
        if apply.returncode:
            failures.append((idx,"failed to update /etc/hosts"))
    if failures:
        for idx,why in failures: print(f"FAIL node{idx}: {why}",file=sys.stderr)
        raise SystemExit(2)
    # Build all-to-all SSH using only public keys; private keys stay node-local.
    pubs=[]
    ensure="set -e; mkdir -p ~/.ssh; chmod 700 ~/.ssh; test -f ~/.ssh/id_ed25519 || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519; cat ~/.ssh/id_ed25519.pub"
    for n in ordered_nodes(cfg):
        idx=int(n["index"]); r=ssh(cfg,idx,ensure,capture=True); pubs.append(r.stdout.strip())
    key_payload="\n".join(pubs)+"\n"; key_b64=base64.b64encode(key_payload.encode()).decode()
    install=("set -e; mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; "
             f"echo {shlex.quote(key_b64)} | base64 -d | while IFS= read -r k; do "
             "[ -z \"$k\" ] || grep -qxF \"$k\" ~/.ssh/authorized_keys || printf '%s\\n' \"$k\" >> ~/.ssh/authorized_keys; done")
    for n in ordered_nodes(cfg): ssh(cfg,int(n["index"]),install)
    try:
        install_cluster_host_trust(cfg, cid)
    except RuntimeError as e:
        raise SystemExit(str(e))
    print("Management naming + all-node SSH public-key mesh + host trust OK")


def cmd_bootstrap_management(args):
    cfg, _ = load_cfg(args.cluster)
    bootstrap_management(cfg, args.cluster)


def bootstrap_storage(cfg, cluster_id: str):
    user = ssh_user(cfg)
    root = configured_model_root(cfg)
    failures = []
    for n in ordered_nodes(cfg):
        idx = int(n["index"])
        marker = json.dumps(storage_marker_expected(cfg, cluster_id, idx), sort_keys=True) + "\n"
        marker_b64 = base64.b64encode(marker.encode()).decode()
        script = f'''set -euo pipefail
expected_user={shlex.quote(user)}
model_root={shlex.quote(root)}
actual_user=$(id -un)
if [[ "$actual_user" != "$expected_user" ]]; then
  echo "expected login user $expected_user but running as $actual_user" >&2
  exit 31
fi
primary_group=$(id -gn "$expected_user")
sudo -n install -d -m 0755 -o "$expected_user" -g "$primary_group" "$model_root"
test -d "$model_root"
test -w "$model_root"
probe="$model_root/.dgx-spark-fleet-write-test.$$"
printf 'storage-write-test\n' > "$probe"
rm -f "$probe"
sudo -n install -d -m 0755 /etc/dgx-spark-fleet
echo {shlex.quote(marker_b64)} | base64 -d | sudo -n tee /etc/dgx-spark-fleet/node.json >/dev/null
sudo -n chmod 0644 /etc/dgx-spark-fleet/node.json
printf 'storage: '
stat -c '%U:%G %a %n' "$model_root"
df -h "$model_root" | tail -n 1
'''
        print(f"\n== storage bootstrap node{idx}: {mng_name(cfg, idx)} ==")
        r = ssh(cfg, idx, script, check=False)
        if r.returncode:
            failures.append(idx)
    if failures:
        raise SystemExit(f"Storage bootstrap failed on nodes: {failures}")
    require_storage_ready(cfg, cluster_id, [int(n["index"]) for n in ordered_nodes(cfg)])
    print(f"Storage bootstrap OK on all nodes: {root}")


def cmd_bootstrap_storage(args):
    cfg, _ = load_cfg(args.cluster)
    bootstrap_storage(cfg, args.cluster)


REMOTE_FABRIC = r'''#!/usr/bin/env python3
import json, re, subprocess, sys
cfg=json.loads(sys.argv[1])
out=subprocess.check_output(["ibdev2netdev"], text=True)
up=[]
for line in out.splitlines():
    m=re.match(r"(\S+)\s+port\s+\d+\s+==>\s+(\S+)\s+\((Up|Down)\)", line.strip())
    if m and m.group(3)=="Up": up.append((m.group(1),m.group(2)))
if len(up)!=2:
    print("Expected exactly two Up CX-7 logical interfaces for one connected physical QSFP port.", file=sys.stderr)
    print(out, file=sys.stderr)
    sys.exit(20)
# Stable ordering by netdev name; both peers must expose the same pair for standard DGX Spark topology.
up=sorted(up,key=lambda x:(0 if x[1].startswith("enp") else 1,x[1]))
lines=["network:","  version: 2","  ethernets:"]
for (ib,dev),addr in zip(up,[cfg["primary"],cfg["secondary"]]):
    lines += [f"    {dev}:","      dhcp4: false","      dhcp6: false","      optional: true","      addresses:",f"        - {addr}"]
text="\n".join(lines)+"\n"
p="/tmp/40-dgx-spark-fabric.yaml"
open(p,"w").write(text)
meta="\n".join([
    f"FABRIC1_IF={up[0][1]}", f"FABRIC1_IB={up[0][0]}", f"FABRIC1_ADDR={cfg['primary']}",
    f"FABRIC2_IF={up[1][1]}", f"FABRIC2_IB={up[1][0]}", f"FABRIC2_ADDR={cfg['secondary']}", ""
])
open("/tmp/dgx-spark-fabric.env","w").write(meta)
subprocess.check_call(["sudo","-n","install","-d","-m","0755","/etc/dgx-spark-fleet"])
subprocess.check_call(["sudo","-n","install","-m","0600",p,"/etc/netplan/40-dgx-spark-fabric.yaml"])
subprocess.check_call(["sudo","-n","install","-m","0644","/tmp/dgx-spark-fabric.env","/etc/dgx-spark-fleet/fabric.env"])
subprocess.check_call(["sudo","-n","netplan","generate"])
subprocess.check_call(["sudo","-n","netplan","apply"])
print(meta,end="")
'''


def cmd_bootstrap_fabric(args):
    cfg, _ = load_cfg(args.cluster)
    # Naming must work before fabric bootstrap because subsequent validation uses it.
    failures=[]
    for n in ordered_nodes(cfg):
        idx=int(n["index"])
        payload={
            "primary": f"{fabric_ip(cfg,idx,1)}/{ipaddress.ip_network(cfg['cluster']['fabric_primary'],strict=False).prefixlen}",
            "secondary": f"{fabric_ip(cfg,idx,2)}/{ipaddress.ip_network(cfg['cluster']['fabric_secondary'],strict=False).prefixlen}",
        }
        cmd = f"python3 - {shlex.quote(__import__('json').dumps(payload))}"
        # Send program on stdin, execute through management network.
        user=ssh_user(cfg); host=n["management"]
        if is_local_address(host):
            r=run([sys.executable,"-",__import__('json').dumps(payload)],check=False,input_text=REMOTE_FABRIC)
        else:
            r=run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=7",f"{user}@{host}",cmd],check=False,input_text=REMOTE_FABRIC)
        if r.returncode: failures.append(idx)
    if failures:
        raise SystemExit(f"Fabric bootstrap failed on nodes: {failures}")
    print("Fabric configuration applied on all nodes")
    validate(cfg, cluster_id=args.cluster, stage="fabric", topology_name=None)


def _validate_remote_check(cfg, idx: int, label: str, command: str, *, show_output=False):
    r = ssh(cfg, idx, command, check=False, capture=True)
    if r.returncode == 0:
        print(f"  OK   {label}")
        if show_output and r.stdout.strip():
            for line in r.stdout.strip().splitlines():
                print(f"       {line}")
        return True
    print(f"  FAIL {label}")
    detail = (r.stderr or r.stdout or "").strip()
    if detail:
        for line in detail.splitlines()[-8:]:
            print(f"       {line}")
    return False


def validate(cfg, *, cluster_id=None, stage="full", topology_name=None):
    cluster_id = cluster_id or cfg["cluster"]["id"]
    nodes = ordered_nodes(cfg) if not topology_name else [node(cfg, i) for i in topology(cfg, topology_name)["nodes"]]
    user = ssh_user(cfg)
    failures = []
    for n in nodes:
        idx = int(n["index"])
        print(f"\n== validate node{idx} ({mng_name(cfg, idx)}) ==")

        checks = [("passwordless sudo", "sudo -n true")]
        for other in ordered_nodes(cfg):
            oidx = int(other["index"])
            checks.append((f"management name {mng_name(cfg, oidx)}", f"getent hosts {shlex.quote(mng_name(cfg, oidx))}"))

        if stage in ("storage", "full"):
            root = configured_model_root(cfg)
            checks += [
                ("storage directory", f"test -d {shlex.quote(root)}"),
                ("storage writable", f"test -w {shlex.quote(root)}"),
                ("storage bootstrap marker", "test -r /etc/dgx-spark-fleet/node.json"),
            ]

        if stage in ("fabric", "full"):
            prefix1 = ipaddress.ip_network(cfg["cluster"]["fabric_primary"], strict=False).prefixlen
            prefix2 = ipaddress.ip_network(cfg["cluster"]["fabric_secondary"], strict=False).prefixlen
            expected1 = f"{fabric_ip(cfg, idx, 1)}/{prefix1}"
            expected2 = f"{fabric_ip(cfg, idx, 2)}/{prefix2}"
            checks += [
                ("fabric bootstrap state", "test -r /etc/dgx-spark-fleet/fabric.env"),
                ("primary fabric address", ". /etc/dgx-spark-fleet/fabric.env; test \"$FABRIC1_ADDR\" = " + shlex.quote(expected1) + "; ip -4 -br addr show \"$FABRIC1_IF\" | grep -Fq " + shlex.quote(fabric_ip(cfg, idx, 1))),
                ("secondary fabric address", ". /etc/dgx-spark-fleet/fabric.env; test \"$FABRIC2_ADDR\" = " + shlex.quote(expected2) + "; ip -4 -br addr show \"$FABRIC2_IF\" | grep -Fq " + shlex.quote(fabric_ip(cfg, idx, 2))),
                ("RDMA command/link state", "command -v rdma >/dev/null && rdma link"),
                ("ibdev2netdev mapping", "command -v ibdev2netdev >/dev/null && ibdev2netdev"),
                ("Docker access", "docker version >/dev/null"),
                ("NVIDIA GPU", "nvidia-smi -L"),
            ]

        for label, command in checks:
            if not _validate_remote_check(cfg, idx, label, command, show_output=False):
                failures.append((idx, label))

        if stage in ("storage", "full"):
            ok, why = storage_marker_status(cfg, cluster_id, idx)
            if ok:
                print("  OK   storage marker matches inventory")
            else:
                print(f"  FAIL storage marker matches inventory: {why}")
                failures.append((idx, why))

    # Requirement: any node reaches any selected peer by stable name using passwordless SSH.
    if stage == "full":
        indexes = [int(n["index"]) for n in nodes]
        for src in indexes:
            print(f"\n== SSH mesh from node{src} ==")
            for dst in indexes:
                if src == dst:
                    continue
                for prefix in ("mng", "con"):
                    target = mng_name(cfg, dst) if prefix == "mng" else con_name(cfg, dst)
                    remote = f"ssh -o BatchMode=yes -o ConnectTimeout=5 {shlex.quote(user+'@'+target)} true"
                    r = ssh(cfg, src, remote, check=False, capture=True)
                    label = f"SSH to {target}"
                    if r.returncode == 0:
                        print(f"  OK   {label}")
                    else:
                        print(f"  FAIL {label}")
                        detail = (r.stderr or r.stdout or "").strip()
                        if detail:
                            print(f"       {detail.splitlines()[-1]}")
                        failures.append((src, label))
    if failures:
        print("\nValidation failures:", file=sys.stderr)
        for x in failures:
            print(f"  node{x[0]}: {x[1]}", file=sys.stderr)
        raise SystemExit(3)
    print("\nVALIDATION OK")


def cmd_validate(args):
    cfg,_=load_cfg(args.cluster)
    validate(cfg,cluster_id=args.cluster,stage=args.stage,topology_name=args.topology)


def cmd_topology(args):
    cfg,_=load_cfg(args.cluster)
    selected_name = getattr(args, "name_opt", None) or getattr(args, "name", None)
    if args.action=="list":
        active=get_active_topology(args.cluster)
        for name,t in sorted(cfg.get("topologies",{}).items()):
            mark="*" if name==active else " "
            print(f"{mark} {name:12} tp={t.get('tp',len(t.get('nodes',[])))} nodes={t.get('nodes',[])}")
    elif args.action=="set":
        if not selected_name:
            raise SystemExit("topology set requires NAME (use --name NAME)")
        t=topology(cfg,selected_name)
        set_active_topology(args.cluster,selected_name)
        print(f"Active topology: {selected_name} (tp={t['tp']}, nodes={t['nodes']})")
    elif args.action=="current":
        print(get_active_topology(args.cluster) or "<none>")


def resolve_topology_arg(cfg, cid, name):
    chosen=name or get_active_topology(cid)
    if not chosen: raise SystemExit("No topology selected; use 'topology set NAME' or --topology NAME")
    return topology(cfg,chosen)


def cmd_model_sync(args):
    """Legacy explicit-path sync kept for compatibility.

    New operator workflows should use ``model-reconcile`` so model identity comes
    from the pinned recipe rather than operator input.
    """
    cfg,_=load_cfg(args.cluster); t=resolve_topology_arg(cfg,args.cluster,args.topology)
    srcidx=args.source_node or t["nodes"][0]
    model_path=checked_model_path(cfg,args.path)
    require_storage_ready(cfg, args.cluster, list(t["nodes"]) + [srcidx])
    r=ssh(cfg,srcidx,f"test -d {shlex.quote(model_path)}",check=False)
    if r.returncode: raise SystemExit(f"Model path missing on source node{srcidx}: {model_path}")
    for dst in t["nodes"]:
        if dst==srcidx: continue
        _sync_directory_over_fabric(cfg, srcidx, model_path, dst, model_path)
    print("MODEL SYNC OK (legacy explicit-path mode)")


def _recipe_enabled(recipe):
    return bool(recipe.get("enabled", True))


def selected_recipe_records(selectors):
    requested = list(selectors or [])
    if not requested:
        requested = ["all"]
    if "all" in requested and len(requested) != 1:
        raise SystemExit("--recipe all cannot be combined with explicit recipe names")
    records = list(recipe_files())
    if requested == ["all"]:
        return [(p,r) for p,r in records if _recipe_enabled(r)]
    wanted=[]
    seen=set()
    byid={r.get("id"):(p,r) for p,r in records}
    for rid in requested:
        if rid in seen: continue
        seen.add(rid)
        if rid not in byid: raise SystemExit(f"Unknown recipe {rid!r}")
        p,r=byid[rid]
        if not _recipe_enabled(r):
            raise SystemExit(f"Recipe {rid!r} is disabled")
        wanted.append((p,r))
    return wanted


def _recipe_source_path(recipe):
    raw = str(recipe.get("source", "")).strip()
    if not raw:
        raise SystemExit(f"Recipe {recipe['id']}: missing source path")
    source = (ROOT / raw).resolve()
    try:
        source.relative_to(ROOT.resolve())
    except ValueError as e:
        raise SystemExit(f"Recipe {recipe['id']}: source escapes repository: {raw!r}") from e
    return source


def _source_initialized(source: Path):
    return source.is_dir() and any(source.iterdir())


def ensure_recipe_sources(records):
    """Initialize missing pinned recipe submodules on the management host.

    Only recipe source code is fetched here. Model artifacts are never downloaded
    by this step. GIT_LFS_SKIP_SMUDGE prevents a recipe repository from pulling
    large LFS payloads into the management checkout.
    """
    missing=[]
    seen=set()
    for _, recipe in records:
        source=_recipe_source_path(recipe)
        if _source_initialized(source):
            continue
        rel=str(source.relative_to(ROOT.resolve()))
        if rel not in seen:
            seen.add(rel)
            missing.append((recipe["id"], rel, source))
    if not missing:
        return

    print("Initializing pinned recipe sources on management host:")
    for rid, rel, _ in missing:
        print(f"  {rid}: {rel}")
    cmd=[
        "env",
        "GIT_LFS_SKIP_SMUDGE=1",
        "GIT_TERMINAL_PROMPT=0",
        "git", "-C", str(ROOT),
        "submodule", "update", "--init", "--recursive", "--",
        *[rel for _,rel,_ in missing],
    ]
    r=run(cmd, check=False)
    if r.returncode:
        raise SystemExit(
            "Failed to initialize pinned recipe sources. "
            "Check management-host GitHub/network access and submodule configuration."
        )
    still=[(rid,rel) for rid,rel,source in missing if not _source_initialized(source)]
    if still:
        details=", ".join(f"{rid} ({rel})" for rid,rel in still)
        raise SystemExit(f"Pinned recipe source remained uninitialized after git submodule update: {details}")


def _artifact_provider_manifest(recipe):
    source = _recipe_source_path(recipe)
    provider = ROOT / recipe.get("artifact_provider", "")
    if not _source_initialized(source):
        raise SystemExit(
            f"Recipe {recipe['id']}: pinned source is not initialized after automatic bootstrap: "
            f"{source.relative_to(ROOT)}"
        )
    if not provider.exists():
        raise SystemExit(f"Recipe {recipe['id']}: missing artifact provider {provider}")
    r = run([sys.executable, str(provider), "--source", str(source), "--recipe-id", recipe["id"]], capture=True, check=False)
    if r.returncode:
        raise SystemExit(f"Recipe {recipe['id']}: artifact provider failed")
    try:
        data=json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Recipe {recipe['id']}: artifact provider returned invalid JSON: {e}") from e
    if not isinstance(data.get("artifacts"), list):
        raise SystemExit(f"Recipe {recipe['id']}: artifact provider returned no artifact list")
    return data


def _artifact_identity(a):
    if a.get("kind") != "huggingface" or not a.get("repo"):
        raise SystemExit(f"Unsupported artifact descriptor: {a}")
    fmt=a.get("format") or "safetensors-directory"
    return f"hf://{a['repo']}#{fmt}"


def merge_recipe_artifacts(records):
    """Return deduplicated artifact requirements derived from current pinned recipes."""
    merged={}
    for _,recipe in records:
        manifest=_artifact_provider_manifest(recipe)
        for raw in manifest["artifacts"]:
            a=dict(raw)
            key=_artifact_identity(a)
            a.setdefault("revision", "")
            a.setdefault("format", "safetensors-directory")
            a.setdefault("mode", "full")
            a.setdefault("files", [])
            a.setdefault("expected_shards", 0)
            a.setdefault("legacy_names", [])
            a["recipes"]=[recipe["id"]]
            if key not in merged:
                merged[key]=a
                continue
            cur=merged[key]
            if cur.get("format") != a.get("format"):
                raise SystemExit(
                    f"Conflicting artifact formats for {a['repo']}: {cur.get('format')} vs {a.get('format')}"
                )
            if cur.get("revision") and a.get("revision") and cur["revision"] != a["revision"]:
                raise SystemExit(
                    f"Conflicting pinned revisions for {key}: {cur['revision']} vs {a['revision']}"
                )
            cur["revision"] = cur.get("revision") or a.get("revision", "")
            cur["recipes"] = sorted(set(cur["recipes"] + a["recipes"]))
            cur["legacy_names"] = sorted(set(cur.get("legacy_names", []) + a.get("legacy_names", [])))
            cur["expected_shards"] = max(int(cur.get("expected_shards",0) or 0), int(a.get("expected_shards",0) or 0))
            if cur.get("mode") == "full" or a.get("mode") == "full":
                cur["mode"]="full"
                cur["files"]=[]
            else:
                cur["files"] = sorted(set(cur.get("files", []) + a.get("files", [])))
    return [merged[k] for k in sorted(merged)]


def artifact_canonical_path(cfg, artifact):
    root=configured_model_root(cfg)
    fmt=artifact.get("format") or "safetensors-directory"
    if fmt != "safetensors-directory":
        raise SystemExit(f"Unsupported artifact format for canonical path: {fmt!r}")
    parts=artifact["repo"].split("/")
    if len(parts) != 2:
        raise SystemExit(f"Unsupported Hugging Face repository name: {artifact['repo']!r}")
    for part in parts:
        safe_name(part)
    return posixpath.join(root, "hf", parts[0], parts[1])


REMOTE_MODEL_INVENTORY = r'''
import json, os, re, sys
root=os.path.normpath(sys.argv[1])
MAX_DEPTH=6
MAX_TEXT=512*1024
MODEL_EXTS=(".safetensors", ".gguf", ".bin", ".pt", ".pth")
entries=[]
seen=set()

def safe_read(path, limit=MAX_TEXT):
    try:
        with open(path,"r",errors="replace") as f:
            return f.read(limit)
    except (OSError, UnicodeError):
        return ""

def file_names(path):
    try:
        return sorted(f for f in os.listdir(path) if os.path.isfile(os.path.join(path,f)))
    except OSError:
        return []

def hf_commits(path):
    out=set()
    cache=os.path.join(path,".cache","huggingface","download")
    if not os.path.isdir(cache): return []
    for cur, dirs, files in os.walk(cache):
        rel=os.path.relpath(cur,cache)
        depth=0 if rel=="." else rel.count(os.sep)+1
        if depth>4:
            dirs[:]=[]
            continue
        for name in files:
            if not name.endswith(".metadata"): continue
            text=safe_read(os.path.join(cur,name),4096)
            for line in text.splitlines()[:3]:
                value=line.strip()
                if re.fullmatch(r"[0-9a-fA-F]{40}",value): out.add(value.lower())
    return sorted(out)

def inspect_dir(path, display_path=None):
    actual=os.path.realpath(path)
    key=(display_path or path,actual)
    if key in seen or not os.path.isdir(actual): return
    names=file_names(actual)
    name_set=set(names)
    config_text=safe_read(os.path.join(actual,"config.json")) if "config.json" in name_set else ""
    readme_text=safe_read(os.path.join(actual,"README.md")) if "README.md" in name_set else ""
    index_text=safe_read(os.path.join(actual,"model.safetensors.index.json"),2*MAX_TEXT) if "model.safetensors.index.json" in name_set else ""
    safetensors=[f for f in names if f.endswith(".safetensors")]
    model_shards=[f for f in names if re.match(r"^model-\d+-of-\d+\.safetensors$",f)]
    ggufs=[f for f in names if f.lower().endswith(".gguf")]
    weights=[f for f in names if f.lower().endswith(MODEL_EXTS)]
    modelish=bool(config_text or index_text or safetensors)
    if not modelish: return
    idx_expected=[]
    if index_text:
        try:
            data=json.loads(index_text)
            idx_expected=sorted(set((data.get("weight_map") or {}).values()))
        except Exception:
            pass
    idx_present=sum(os.path.isfile(os.path.join(actual,f)) for f in idx_expected)
    cfg={}
    if config_text:
        try:
            raw=json.loads(config_text)
            for k in ("_name_or_path","name_or_path","model_type","architectures","quantization_config"):
                if k in raw: cfg[k]=raw[k]
        except Exception:
            pass
    repo_hints=set()
    hay="\n".join([config_text,readme_text,index_text])
    for m in re.finditer(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])",hay):
        repo_hints.add(m.group(1).strip(".,;:()[]{}"))
    total=0
    for name in names:
        try: total+=os.path.getsize(os.path.join(actual,name))
        except OSError: pass
    seen.add(key)
    entries.append({
        "path": os.path.normpath(display_path or path),
        "realpath": actual,
        "type":"directory",
        "basename":os.path.basename(os.path.normpath(display_path or path)),
        "bytes":total,
        "files":names,
        "config":cfg,
        "repo_hints":sorted(repo_hints),
        "hf_commits":hf_commits(actual),
        "has_config":bool(config_text),
        "has_index":bool(index_text),
        "index_expected":len(idx_expected),
        "index_present":idx_present,
        "safetensors":len(safetensors),
        "model_shards":len(model_shards),
        "ggufs":len(ggufs),
        "weights":len(weights),
    })

if os.path.isdir(root):
    for cur, dirs, files in os.walk(root,followlinks=False):
        rel=os.path.relpath(cur,root)
        depth=0 if rel=="." else rel.count(os.sep)+1
        dirs[:]=[d for d in dirs if d not in {".cache",".git","__pycache__"}]
        if depth>MAX_DEPTH:
            dirs[:]=[]
            continue
        inspect_dir(cur)
        for d in list(dirs):
            q=os.path.join(cur,d)
            if os.path.islink(q) and os.path.isdir(q): inspect_dir(q,q)
    for cur, dirs, files in os.walk(root,followlinks=False):
        rel=os.path.relpath(cur,root)
        depth=0 if rel=="." else rel.count(os.sep)+1
        dirs[:]=[d for d in dirs if d not in {".cache",".git","__pycache__"}]
        if depth>MAX_DEPTH:
            dirs[:]=[]
            continue
        for name in files:
            if not name.lower().endswith(".gguf"): continue
            q=os.path.join(cur,name)
            try: size=os.path.getsize(q)
            except OSError: size=0
            entries.append({"path":q,"realpath":os.path.realpath(q),"type":"gguf","basename":name,
                            "bytes":size,"files":[name],"config":{},"repo_hints":[],"hf_commits":[],
                            "has_config":False,"has_index":False,"index_expected":0,"index_present":0,
                            "safetensors":0,"model_shards":0,"ggufs":1,"weights":1})
print(json.dumps({"root":root,"entries":entries},sort_keys=True))
'''


def inventory_model_root_on_node(cfg, idx):
    root=configured_model_root(cfg)
    user=ssh_user(cfg); host=node(cfg,idx)["management"]
    cmd=f"python3 - {shlex.quote(root)}"
    if is_local_address(host):
        r=run([sys.executable,"-",root],check=False,capture=True,input_text=REMOTE_MODEL_INVENTORY)
    else:
        r=run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=7",f"{user}@{host}",cmd],check=False,capture=True,input_text=REMOTE_MODEL_INVENTORY)
    if r.returncode:
        raise SystemExit(f"node{idx}: could not inventory model_root {root}")
    try:
        data=json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        raise SystemExit(f"node{idx}: invalid model inventory response: {e}") from e
    if not isinstance(data.get("entries"),list):
        raise SystemExit(f"node{idx}: invalid model inventory response")
    return data["entries"]


def _norm_model_token(value):
    return re.sub(r"[^a-z0-9]+","",str(value).lower())


def _entry_completeness(entry, artifact):
    if entry.get("type") != "directory":
        return False, 0, 0, "recipe requires a directory checkpoint"
    files=set(entry.get("files") or [])
    if artifact.get("mode")=="subset":
        required=list(artifact.get("files") or [])
        present=sum(name in files for name in required)
        return bool(required) and present==len(required),present,len(required),"required subset files"
    expected=int(artifact.get("expected_shards") or 0)
    if entry.get("has_index") and int(entry.get("index_expected") or 0)>0:
        exp=int(entry.get("index_expected") or 0); present=int(entry.get("index_present") or 0)
        complete=bool(entry.get("has_config")) and present==exp
        if expected and int(entry.get("model_shards") or 0) not in (0,expected) and exp != expected:
            complete=False
        return complete,present,exp,"safetensors index"
    if expected:
        present=int(entry.get("model_shards") or 0)
        return bool(entry.get("has_config")) and present>=expected,present,expected,"model shard count"
    present=int(entry.get("weights") or 0)
    return bool(entry.get("has_config")) and present>0,present,1,"config + weight files"


def _artifact_entry_match(entry, artifact, canonical):
    fmt=artifact.get("format") or "safetensors-directory"
    if fmt == "safetensors-directory":
        if entry.get("type") != "directory":
            return {"score":0,"strong":False,"reasons":["incompatible artifact format"],"complete_hint":False,
                    "present":0,"expected":0,"completeness_reason":"recipe requires a safetensors directory checkpoint"}
        # A directory containing only GGUF weights is a different checkpoint format,
        # even if its name resembles the Hugging Face repository name.
        if int(entry.get("ggufs") or 0) > 0 and int(entry.get("safetensors") or 0) == 0 and not entry.get("has_index"):
            return {"score":0,"strong":False,"reasons":["incompatible GGUF-only directory"],"complete_hint":False,
                    "present":0,"expected":0,"completeness_reason":"recipe requires a safetensors directory checkpoint"}
    else:
        return {"score":0,"strong":False,"reasons":[f"unsupported artifact format {fmt}"],"complete_hint":False,
                "present":0,"expected":0,"completeness_reason":"unsupported artifact format"}
    repo=artifact["repo"]; repo_base=repo.rsplit("/",1)[-1]
    legacy=set(artifact.get("legacy_names") or []) | {repo_base}
    path=posixpath.normpath(str(entry.get("path", "")))
    base=str(entry.get("basename", ""))
    config=entry.get("config") or {}
    repo_hints={str(x).lower() for x in (entry.get("repo_hints") or [])}
    commits={str(x).lower() for x in (entry.get("hf_commits") or [])}
    reasons=[]; score=0; strong=False
    if path==canonical:
        score+=120; strong=True; reasons.append("canonical path")
    if base in legacy:
        score+=100; strong=True; reasons.append("exact recipe/legacy directory name")
    cfg_names=[]
    for key in ("_name_or_path","name_or_path"):
        value=config.get(key)
        if isinstance(value,str) and value.strip(): cfg_names.append(value.strip())
    if any(v.lower()==repo.lower() or posixpath.basename(v).lower()==repo_base.lower() for v in cfg_names):
        score+=110; strong=True; reasons.append("config model identity")
    if repo.lower() in repo_hints:
        score+=100; strong=True; reasons.append("embedded Hugging Face repository id")
    elif repo_base.lower() in {x.rsplit('/',1)[-1] for x in repo_hints}:
        score+=80; strong=True; reasons.append("embedded repository basename")
    revision=str(artifact.get("revision") or "").lower()
    if revision and revision in commits:
        score+=120; strong=True; reasons.append("Hugging Face revision metadata")
    base_token=_norm_model_token(base); repo_token=_norm_model_token(repo_base)
    if repo_token and base_token and (repo_token in base_token or base_token in repo_token):
        score+=45; reasons.append("similar directory name")
    complete,present,expected,why=_entry_completeness(entry,artifact)
    if artifact.get("mode")=="subset" and expected and present:
        score+=min(35,10+present*5); reasons.append(f"{present}/{expected} recipe subset files")
    elif int(artifact.get("expected_shards") or 0)>0:
        need=int(artifact.get("expected_shards") or 0); have=int(entry.get("model_shards") or 0)
        if have:
            if have==need: score+=35; reasons.append(f"exact {need}-shard structure")
            elif abs(have-need)<=2: score+=15; reasons.append(f"near {need}-shard structure ({have})")
    return {"score":score,"strong":strong,"reasons":reasons,"complete_hint":complete,
            "present":present,"expected":expected,"completeness_reason":why}


def classify_artifact_from_inventory(cfg, artifact, entries):
    canonical=artifact_canonical_path(cfg,artifact)
    ranked=[]
    for entry in entries:
        m=_artifact_entry_match(entry,artifact,canonical)
        if m["score"]<=0: continue
        ranked.append((m["score"],entry,m))
    ranked.sort(key=lambda x:(x[0],x[2]["complete_hint"],x[2]["present"]),reverse=True)
    strong=[x for x in ranked if x[2]["strong"]]
    if strong:
        _,entry,m=strong[0]
        revision=str(artifact.get("revision") or "").lower()
        commits={str(x).lower() for x in (entry.get("hf_commits") or [])}
        revision_verified=(not revision) or revision in commits
        state="complete" if m["complete_hint"] else "partial"
        if revision and commits and revision not in commits:
            state="candidate"
            m["reasons"].append("local Hugging Face revision differs from pinned recipe")
        return {"state":state,"path":entry["path"],"present":m["present"],"expected":m["expected"],
                "score":m["score"],"reasons":m["reasons"],"bytes":entry.get("bytes",0),
                "revision_verified":revision_verified,"entry":entry}
    candidates=[x for x in ranked if x[0]>=30]
    if candidates:
        _,entry,m=candidates[0]
        return {"state":"candidate","path":entry["path"],"present":m["present"],"expected":m["expected"],
                "score":m["score"],"reasons":m["reasons"],"bytes":entry.get("bytes",0),
                "revision_verified":False,"entry":entry}
    return {"state":"missing","path":canonical,"present":0,"expected":0,"score":0,"reasons":[],"bytes":0,
            "revision_verified":False}


def _inventory_matched_paths(plan):
    used={}
    for entry in plan:
        for idx,probe in entry["probes"].items():
            if probe.get("state") in {"complete","partial","candidate"} and probe.get("path"):
                used.setdefault(idx,set()).add(posixpath.normpath(probe["path"]))
    return used


def probe_artifact_on_node(cfg, idx, artifact):
    entries=inventory_model_root_on_node(cfg,idx)
    return classify_artifact_from_inventory(cfg,artifact,entries)

def _sync_directory_over_fabric(cfg, srcidx, source_path, dstidx, destination_path):
    dest_host=f"{ssh_user(cfg)}@{con_name(cfg,dstidx)}"
    remote=(
        f"set -e; ssh -o BatchMode=yes -o ConnectTimeout=7 {shlex.quote(dest_host)} "
        f"mkdir -p {shlex.quote(destination_path)}; "
        f"rsync -aH --partial --info=progress2 {shlex.quote(source_path.rstrip('/') + '/')} "
        f"{shlex.quote(dest_host + ':' + destination_path.rstrip('/') + '/')}"
    )
    print(f"\n== artifact sync node{srcidx} -> node{dstidx} over {con_name(cfg,dstidx)} ==")
    r=ssh(cfg,srcidx,remote,check=False)
    if r.returncode: raise SystemExit(f"Artifact sync failed node{srcidx} -> node{dstidx}")


def _ensure_canonical_alias(cfg, idx, artifact, actual_path):
    canonical=artifact_canonical_path(cfg,artifact)
    if actual_path==canonical: return canonical
    checked_model_path(cfg,actual_path); checked_model_path(cfg,canonical)
    cmd=(
        f"set -e; mkdir -p {shlex.quote(posixpath.dirname(canonical))}; "
        f"if [ ! -e {shlex.quote(canonical)} ] && [ ! -L {shlex.quote(canonical)} ]; then "
        f"ln -s {shlex.quote(actual_path)} {shlex.quote(canonical)}; fi"
    )
    r=ssh(cfg,idx,cmd,check=False)
    if r.returncode: raise SystemExit(f"node{idx}: could not create canonical artifact alias {canonical}")
    return canonical


def _management_hf_token():
    token=os.environ.get("HF_TOKEN","")
    if any(c in token for c in ("\n","\r","\0")):
        raise SystemExit("HF_TOKEN contains an invalid newline or NUL character")
    return token


def _download_artifact_on_node(cfg, idx, artifact, target):
    repo=artifact["repo"]; rev=artifact.get("revision","")
    include=list(artifact.get("files") or []) if artifact.get("mode")=="subset" else []
    token=_management_hf_token()
    token_setup=''
    input_text=None
    if token:
        # Carry the secret over SSH stdin. Never interpolate it into argv/the printed command,
        # and do not call `hf auth login`, which would persist credentials on the Spark.
        token_setup='IFS= read -r HF_TOKEN; export HF_TOKEN; '
        input_text=token + "\n"
    setup=(
        'set -euo pipefail; ' + token_setup +
        'HFV="$HOME/.local/share/dgx-spark-fleet/hf-venv"; '
        'if [ ! -x "$HFV/bin/hf" ]; then '
        'if ! python3 -m venv "$HFV"; then sudo -n apt-get update; sudo -n apt-get install -y python3-venv; python3 -m venv "$HFV"; fi; '
        '"$HFV/bin/pip" install -U huggingface_hub; fi; '
        f'mkdir -p {shlex.quote(target)}; '
    )
    args=['"$HFV/bin/hf"','download',shlex.quote(repo)]
    if rev: args += ['--revision',shlex.quote(rev)]
    for name in include: args += ['--include',shlex.quote(name)]
    args += ['--local-dir',shlex.quote(target)]
    download_command=" ".join(args)
    command=(
        setup +
        'rc=1; for attempt in 1 2 3; do '
        f'if {download_command}; then rc=0; break; else rc=$?; fi; '
        'if [ "$attempt" -lt 3 ]; then '
        'echo "hf download attempt ${attempt}/3 failed (rc=${rc}); retrying resumable download" >&2; '
        'sleep $((attempt * 10)); '
        'else echo "hf download attempt ${attempt}/3 failed (rc=${rc}); giving up" >&2; fi; '
        'done; exit "$rc"'
    )
    auth="authenticated (HF_TOKEN forwarded transiently)" if token else "anonymous (HF_TOKEN not set)"
    print(f"\n== download on node{idx}: {repo}{('@'+rev) if rev else ''} [{auth}] ==")
    r=ssh(cfg,idx,command,check=False,input_text=input_text)
    if r.returncode:
        raise SystemExit(
            f"Download failed on node{idx}. Internet access (and Hugging Face credentials if required) "
            "must be available on the selected Spark seed."
        )


def _refresh_reconcile_entry(entry):
    probes=entry["probes"]
    complete=[idx for idx,p in probes.items() if p.get("state")=="complete"]
    entry["source"]=sorted(complete)[0] if complete else None
    entry["partial"]=[idx for idx,p in probes.items() if p.get("state")=="partial"]
    entry["candidates"]=[idx for idx,p in probes.items() if p.get("state")=="candidate"]


def _suppress_candidates_claimed_by_other_artifacts(cfg, plan):
    """Do not let one positively identified artifact masquerade as another.

    Quantized/derived checkpoints can legitimately embed the upstream Hugging Face
    repository id in config metadata.  That hint is useful for discovering renamed
    artifacts, but it must not make a directory already positively identified for a
    different recipe requirement block reconciliation of the real upstream model.
    """
    claimed={}
    for entry in plan:
        artifact=entry["artifact"]
        identity=_artifact_identity(artifact)
        for idx,probe in entry["probes"].items():
            if probe.get("state") not in {"complete","partial"}:
                continue
            path=probe.get("path")
            if not path:
                continue
            key=posixpath.normpath(path)
            claimed.setdefault(idx,{}).setdefault(key,[]).append(
                {"identity":identity,"repo":artifact["repo"]}
            )

    for entry in plan:
        artifact=entry["artifact"]
        identity=_artifact_identity(artifact)
        canonical=artifact_canonical_path(cfg,artifact)
        for idx,probe in list(entry["probes"].items()):
            if probe.get("state") != "candidate" or not probe.get("path"):
                continue
            path=posixpath.normpath(probe["path"])
            owners=[x for x in claimed.get(idx,{}).get(path,[]) if x["identity"] != identity]
            if not owners:
                continue
            entry["probes"][idx]={
                "state":"missing",
                "path":canonical,
                "present":0,
                "expected":0,
                "score":0,
                "reasons":[],
                "bytes":0,
                "revision_verified":False,
                "ignored_candidate":{
                    "path":path,
                    "owner_repos":sorted({x["repo"] for x in owners}),
                    "candidate_reasons":list(probe.get("reasons") or []),
                },
            }
        _refresh_reconcile_entry(entry)


def build_reconcile_plan(cfg, artifacts, inventories=None):
    indexes=[int(n["index"]) for n in ordered_nodes(cfg)]
    if inventories is None:
        inventories={idx:inventory_model_root_on_node(cfg,idx) for idx in indexes}
    plan=[]
    for artifact in artifacts:
        probes={idx:classify_artifact_from_inventory(cfg,artifact,inventories[idx]) for idx in indexes}
        entry={"artifact":artifact,"probes":probes}
        _refresh_reconcile_entry(entry)
        plan.append(entry)
    _suppress_candidates_claimed_by_other_artifacts(cfg,plan)
    return plan, inventories


def _format_bytes(value):
    value=float(value or 0)
    units=["B","KiB","MiB","GiB","TiB"]
    i=0
    while value>=1024 and i<len(units)-1:
        value/=1024; i+=1
    return f"{value:.1f} {units[i]}" if i else f"{int(value)} B"


def print_model_inventory(inventories, plan):
    matched=_inventory_matched_paths(plan)
    print("\nExisting model storage inventory:")
    any_entries=False
    for idx in sorted(inventories):
        entries=sorted(inventories[idx],key=lambda e:str(e.get("path","")))
        print(f"  node{idx}:")
        if not entries:
            print("      <no recognizable model artifacts>")
            continue
        any_entries=True
        for e in entries:
            p=posixpath.normpath(str(e.get("path","")))
            role="matched" if p in matched.get(idx,set()) else "UNMATCHED"
            details=[]
            if e.get("type")=="gguf": details.append("GGUF")
            else:
                if e.get("model_shards"): details.append(f"{e['model_shards']} model shards")
                elif e.get("safetensors"): details.append(f"{e['safetensors']} safetensors")
                if e.get("ggufs"): details.append(f"{e['ggufs']} GGUF")
                if e.get("has_config"): details.append("config")
                if e.get("has_index"): details.append("index")
            detail=("; "+", ".join(details)) if details else ""
            print(f"      {role:9} {_format_bytes(e.get('bytes',0)):>10}  {p}{detail}")
    if not any_entries:
        print("  no recognizable model data found")


def unresolved_candidate_entries(plan):
    out=[]
    for entry in plan:
        if entry.get("source") is not None:
            continue
        for idx,probe in entry["probes"].items():
            if probe.get("state")=="candidate":
                out.append((entry["artifact"],idx,probe))
    return out

def _choose_download_seed(entry, requested_seed=None):
    probes=entry["probes"]
    if requested_seed is not None:
        if requested_seed not in probes: raise SystemExit(f"--seed-node {requested_seed} is not in this cluster")
        return requested_seed
    partial=[(int(v.get("present",0)),idx) for idx,v in probes.items() if v.get("state")=="partial"]
    if partial:
        return sorted(partial,reverse=True)[0][1]
    return sorted(probes)[0]


def print_reconcile_plan(cluster_id, records, plan, inventories, *, download_missing=False):
    print(f"Cluster: {cluster_id}")
    print("Recipes: " + ", ".join(r["id"] for _,r in records))
    print_model_inventory(inventories,plan)
    print("\nArtifact requirements (deduplicated):")
    for n,entry in enumerate(plan,1):
        a=entry["artifact"]
        rev=f"@{a['revision']}" if a.get("revision") else ""
        print(f"  [{n}] {a['repo']}{rev}")
        print(f"      format: {a.get('format','safetensors-directory')}")
        print(f"      recipes: {', '.join(a['recipes'])}")
        for idx in sorted(entry["probes"]):
            p=entry["probes"][idx]
            detail=""
            if p.get("expected"): detail=f" ({p.get('present',0)}/{p.get('expected')})"
            rev_note=""
            if a.get("revision") and p.get("state") in {"complete","partial"} and not p.get("revision_verified"):
                rev_note=" [revision metadata unavailable]"
            reason=""
            if p.get("state")=="candidate" and p.get("reasons"):
                reason="; " + ", ".join(p["reasons"][:3])
            elif p.get("ignored_candidate"):
                ignored=p["ignored_candidate"]
                owners=", ".join(ignored.get("owner_repos") or [])
                reason=f"; ignored candidate {ignored.get('path')} (already identified as {owners})"
            print(f"      node{idx}: {p.get('state','?'):9} {p.get('path','')}{detail}{rev_note}{reason}")
    print("\nPlan:")
    actions=0
    for entry in plan:
        a=entry["artifact"]
        candidates=[idx for idx,p in entry["probes"].items() if p.get("state")=="candidate"]
        if entry["source"] is not None:
            missing=[idx for idx,p in entry["probes"].items() if p.get("state") in {"missing","partial"}]
            if missing:
                print(f"  COPY {a['repo']}: node{entry['source']} -> " + ", ".join(f"node{i}" for i in missing))
                actions += 1
            if candidates:
                print(f"  REVIEW {a['repo']}: candidate data on " + ", ".join(f"node{i}" for i in candidates) + "; no duplicate copy there until identified")
                actions += 1
            if not missing and not candidates:
                print(f"  READY {a['repo']}: complete on all nodes")
        elif candidates:
            details=[]
            for idx in candidates:
                p=entry["probes"][idx]
                details.append(f"node{idx} {p.get('path')}")
            print(f"  REVIEW {a['repo']}: plausible existing artifact at " + "; ".join(details))
            print("         Internet download is blocked for this artifact until it can be positively identified.")
            actions += 1
        else:
            seed=_choose_download_seed(entry)
            verb="DOWNLOAD" if download_missing else "MISSING"
            print(f"  {verb} {a['repo']}: seed node{seed}; then replicate to all nodes")
            actions += 1
    if not actions: print("  no changes required")

def _ensure_reconcile_tools(cfg, indexes):
    for idx in indexes:
        r=ssh(cfg,idx,"command -v rsync >/dev/null",check=False)
        if r.returncode:
            print(f"node{idx}: installing rsync required by model reconciliation")
            r=ssh(cfg,idx,"sudo -n apt-get update && sudo -n apt-get install -y rsync",check=False)
            if r.returncode: raise SystemExit(f"node{idx}: could not install rsync")


def cmd_model_reconcile(args):
    cfg,_=load_cfg(args.cluster)
    indexes=[int(n["index"]) for n in ordered_nodes(cfg)]
    require_storage_ready(cfg,args.cluster,indexes)
    records=selected_recipe_records(args.recipe)
    ensure_recipe_sources(records)
    artifacts=merge_recipe_artifacts(records)
    if not artifacts: raise SystemExit("Selected recipes expose no model artifacts")
    plan,inventories=build_reconcile_plan(cfg,artifacts)
    print_reconcile_plan(args.cluster,records,plan,inventories,download_missing=args.download_missing)
    if args.download_missing and not args.apply:
        raise SystemExit("--download-missing requires --apply")
    if not args.apply:
        print("\nREAD-ONLY PLAN. Add --apply to synchronize positively identified existing artifacts; add --download-missing to permit Internet downloads on a Spark seed.")
        return
    _ensure_reconcile_tools(cfg,indexes)
    remaining_missing=False
    unresolved=False
    for entry in plan:
        a=entry["artifact"]
        source=entry["source"]
        candidates=[idx for idx,p in entry["probes"].items() if p.get("state")=="candidate"]
        if source is None:
            if candidates:
                unresolved=True
                print(f"SKIP {a['repo']}: unresolved existing candidate blocks download/copy")
                continue
            if not args.download_missing:
                remaining_missing=True
                continue
            source=_choose_download_seed(entry,args.seed_node)
            probe=entry["probes"][source]
            target=probe.get("path") if probe.get("state")=="partial" else artifact_canonical_path(cfg,a)
            _download_artifact_on_node(cfg,source,a,target)
            fresh=probe_artifact_on_node(cfg,source,a)
            if fresh.get("state")!="complete":
                raise SystemExit(f"node{source}: downloaded artifact still fails completeness/identity check: {a['repo']}")
            entry["probes"][source]=fresh
        source_path=entry["probes"][source].get("path") or artifact_canonical_path(cfg,a)
        _ensure_canonical_alias(cfg,source,a,source_path)
        canonical=artifact_canonical_path(cfg,a)
        for dst in sorted(entry["probes"]):
            if dst==source: continue
            state=entry["probes"][dst].get("state")
            if state=="complete":
                _ensure_canonical_alias(cfg,dst,a,entry["probes"][dst].get("path") or canonical)
                continue
            if state=="candidate":
                unresolved=True
                print(f"SKIP node{dst} {a['repo']}: candidate {entry['probes'][dst].get('path')} requires positive identification")
                continue
            destination=entry["probes"][dst].get("path") if state=="partial" else canonical
            _sync_directory_over_fabric(cfg,source,source_path,dst,destination)
            fresh=probe_artifact_on_node(cfg,dst,a)
            if fresh.get("state")!="complete":
                raise SystemExit(f"node{dst}: artifact incomplete after sync: {a['repo']}")
            _ensure_canonical_alias(cfg,dst,a,fresh.get("path") or destination)
    if unresolved:
        print("\nMODEL RECONCILE PARTIAL: positively identified artifacts were reconciled, but candidate data remains unresolved.")
        print("Rerun the read-only Job 20 and inspect CANDIDATE/UNMATCHED entries before permitting downloads for those artifacts.")
    elif remaining_missing:
        print("\nMODEL RECONCILE PARTIAL: existing artifacts were synchronized; missing artifacts were not downloaded.")
        print("Rerun with --apply --download-missing to fetch the remaining recipe requirements on a Spark seed.")
    else:
        print("\nMODEL RECONCILE OK")



def management_iface(cfg, idx):
    peers=[n for n in ordered_nodes(cfg) if int(n["index"])!=idx]
    if not peers: return ""
    # Raw bootstrap names such as ``spark-2`` only need to resolve on the
    # management workstation. Every Spark resolves the managed mng-* aliases.
    target=mng_name(cfg,int(peers[0]["index"]))
    r=ssh(cfg,idx,f"ip route get {shlex.quote(target)}",capture=True,check=False)
    if r.returncode: return ""
    m=re.search(r"\bdev\s+(\S+)",r.stdout)
    return m.group(1) if m else ""

def read_remote_fabric(cfg, idx):
    r=ssh(cfg,idx,"cat /etc/dgx-spark-fleet/fabric.env",capture=True,check=False)
    if r.returncode:
        raise SystemExit(f"node{idx}: fabric is not bootstrapped")
    vals={}
    for line in r.stdout.splitlines():
        if "=" in line:
            k,v=line.split("=",1); vals[k.strip()]=v.strip()
    required={"FABRIC1_IF","FABRIC1_IB","FABRIC1_ADDR","FABRIC2_IF","FABRIC2_IB","FABRIC2_ADDR"}
    missing=required-set(vals)
    if missing: raise SystemExit(f"node{idx}: incomplete fabric.env: {sorted(missing)}")
    return vals

def generate_legacy_config(cfg,cid,t):
    out=STATE/cid/f"compat-{t['name']}.toml"
    out.parent.mkdir(parents=True,exist_ok=True)
    chosen=[node(cfg,i) for i in t["nodes"]]
    mgmt_if=management_iface(cfg,t["nodes"][0])
    lines=["version = 1","","[cluster]",f'id = "{cid}-{t["name"]}"',f'ssh_user = "{ssh_user(cfg)}"',
           '# generated compatibility inventory; no secrets', f'management_interface = "{mgmt_if}"', 'roce_mtu = 0', '',
           f'[topologies.tp{t["tp"]}]', 'nodes = ['+", ".join(f'"node{i}"' for i in t["nodes"])+']','']
    for pos,n in enumerate(chosen):
        idx=int(n['index']); fab=read_remote_fabric(cfg,idx)
        lines += [f'[nodes.node{idx}]']
        if pos==0: lines += ['local = true']
        lines += [f'ssh_host = "{mng_name(cfg,idx)}"', f'control_ip = "{resolve_management_ipv4(n["management"])}"',
                  f'model_host = "{configured_model_root(cfg)}"','',
                  f'[[nodes.node{idx}.roce]]',f'ifname = "{fab["FABRIC1_IF"]}"',f'ibdev = "{fab["FABRIC1_IB"]}"',f'address = "{fab["FABRIC1_ADDR"]}"','',
                  f'[[nodes.node{idx}.roce]]',f'ifname = "{fab["FABRIC2_IF"]}"',f'ibdev = "{fab["FABRIC2_IB"]}"',f'address = "{fab["FABRIC2_ADDR"]}"','']
    out.write_text("\n".join(lines))
    return out

def recipe_files():
    for p in sorted(RECIPES.glob("*.toml")):
        with p.open("rb") as f: yield p,tomllib.load(f)


def recipe_by_id(rid):
    for p,r in recipe_files():
        if r.get("id")==rid: return p,r
    raise SystemExit(f"Unknown recipe {rid!r}")


def _recipe_profile(recipe, tp):
    selected=(recipe.get("profiles") or {}).get(str(tp),"")
    if not selected:
        raise SystemExit(f"Recipe {recipe['id']} has no profile mapping for tp={tp}")
    path=ROOT / "profiles" / f"{selected}.toml"
    if not path.exists():
        raise SystemExit(f"Recipe {recipe['id']}: missing profile {path}")
    with path.open("rb") as f:
        profile=tomllib.load(f)
    return selected,path,profile


def _recipe_artifacts_ready(cfg, cluster_id, t, recipe):
    """Require Job 20 to have positively identified every recipe artifact."""
    manifest=_artifact_provider_manifest(recipe)
    artifacts=[dict(a) for a in manifest.get("artifacts",[])]
    inventories={idx:inventory_model_root_on_node(cfg,idx) for idx in t["nodes"]}
    failures=[]
    for a in artifacts:
        for idx in t["nodes"]:
            probe=classify_artifact_from_inventory(cfg,a,inventories[idx])
            if probe.get("state")!="complete":
                failures.append((a["repo"],idx,probe.get("state","missing"),probe.get("path","")))
            else:
                _ensure_canonical_alias(cfg,idx,a,probe.get("path") or artifact_canonical_path(cfg,a))
    if failures:
        lines=["Recipe model prerequisites are not reconciled; run Job 20 first:"]
        for repo,idx,state,path in failures:
            suffix=f" ({path})" if path else ""
            lines.append(f"  node{idx}: {repo}: {state}{suffix}")
        raise SystemExit("\n".join(lines))
    return artifacts


def _recipe_runtime_env(cfg, profile, artifacts):
    """Map canonical Job-20 artifacts onto the pinned upstream recipe knobs."""
    adapter=profile.get("adapter")
    env={"DGX_RECONCILED_MODELS":"1"}
    canonical=[(a,artifact_canonical_path(cfg,a)) for a in artifacts]
    if adapter=="mia_exl3":
        model=next(((a,p) for a,p in canonical if a.get("mode","full")=="full" and "EXL3" in a.get("repo","")),None)
        engram=next(((a,p) for a,p in canonical if a.get("mode")=="subset"),None)
        if not model or not engram:
            raise SystemExit("EXL3 recipe artifact manifest does not expose model + Engram artifacts")
        env.update({
            "DGX_MODEL_HOST":model[1],
            "DGX_ENGRAM_DIR":engram[1],
            "DGX_WORKER_MODEL_DIR":model[1],
            "DGX_WORKER_ENGRAM_DIR":engram[1],
        })
    elif adapter=="mia_sglang_tp4":
        if len(canonical)!=1:
            raise SystemExit("SGLang recipe must expose exactly one model artifact")
        env["DGX_MODEL_DIR"]=canonical[0][1]
    elif adapter=="v4_launcher":
        if len(canonical)!=1:
            raise SystemExit("V4 launcher recipe must expose exactly one model artifact")
        env["DGX_V4_MODELS_HOST"]=posixpath.dirname(canonical[0][1])
    else:
        raise SystemExit(f"Unsupported legacy profile adapter {adapter!r}")
    return env



def _runtime_artifact_provider_manifest(recipe):
    raw = str(recipe.get("runtime_artifact_provider", "")).strip()
    if not raw:
        return {"recipe": recipe.get("id", ""), "runtime_artifacts": []}
    source = _recipe_source_path(recipe)
    provider = (ROOT / raw).resolve()
    try:
        provider.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise SystemExit(f"Recipe {recipe['id']}: runtime artifact provider escapes repository: {raw!r}") from exc
    if not provider.exists():
        raise SystemExit(f"Recipe {recipe['id']}: missing runtime artifact provider {provider}")
    r = run(
        [sys.executable, str(provider), "--source", str(source), "--recipe-id", recipe["id"]],
        capture=True,
        check=False,
    )
    if r.returncode:
        detail = (r.stderr or r.stdout or "").strip()
        raise SystemExit(
            f"Recipe {recipe['id']}: runtime artifact provider failed"
            + (f": {detail.splitlines()[-1]}" if detail else "")
        )
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Recipe {recipe['id']}: runtime artifact provider returned invalid JSON: {exc}") from exc
    artifacts = data.get("runtime_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise SystemExit(f"Recipe {recipe['id']}: runtime artifact provider returned no runtime artifacts")
    for artifact in artifacts:
        if artifact.get("kind") != "native-library" or not re.fullmatch(
            r"[0-9a-f]{64}", str(artifact.get("sha256", ""))
        ):
            raise SystemExit(f"Recipe {recipe['id']}: unsupported runtime artifact descriptor: {artifact}")
    return data


def selected_runtime_recipe_records(selectors):
    requested = list(selectors or [])
    explicit = bool(requested and requested != ["all"])
    records = selected_recipe_records(requested)
    runtime = []
    missing = []
    for record in records:
        if record[1].get("runtime_artifact_provider"):
            runtime.append(record)
        elif explicit:
            missing.append(record[1].get("id", "<unknown>"))
    if missing:
        raise SystemExit("Selected recipe(s) do not declare runtime artifacts: " + ", ".join(missing))
    if not runtime:
        raise SystemExit("No enabled recipes declare runtime artifacts")
    return runtime


def runtime_state_path(cluster_id, topology_name, recipe_id):
    return STATE / cluster_id / "runtime" / topology_name / recipe_id / "state.json"


def _load_runtime_state(cluster_id, topology_name, recipe_id):
    path = runtime_state_path(cluster_id, topology_name, recipe_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _write_runtime_state(cluster_id, topology_name, recipe_id, data):
    path = runtime_state_path(cluster_id, topology_name, recipe_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _runtime_artifact_dirs(cfg, artifact):
    sha12 = artifact["sha256"][:12]
    user = ssh_user(cfg)
    home = f"/home/{user}"
    stage = artifact.get("stage") or {}
    host_tmpl = str(stage.get("host_template", "{home}/.cache/dgx-spark-fleet/runtime/{sha12}"))
    container_tmpl = str(
        stage.get("container_template", "/root/.cache/dgx-spark-fleet/runtime/{sha12}")
    )
    host = posixpath.normpath(host_tmpl.format(home=home, sha12=sha12))
    container = posixpath.normpath(container_tmpl.format(home=home, sha12=sha12))
    if host != home and not host.startswith(home.rstrip("/") + "/"):
        raise SystemExit(f"Runtime artifact host path must stay under {home}: {host}")
    if not container.startswith("/"):
        raise SystemExit(f"Runtime artifact container path must be absolute: {container}")
    host_mount_tmpl = str(
        stage.get("host_mount_template", "{home}/.cache/vllm-dsv41-flash-exl3")
    )
    container_mount = str(stage.get("container_mount", "/root/.cache/vllm"))
    host_mount = posixpath.normpath(host_mount_tmpl.format(home=home, sha12=sha12))
    return host, container, host_mount, container_mount


def _runtime_stage_verify_command(artifact, stage):
    runtime = artifact.get("runtime_py") or {}
    overlay = (artifact.get("stage") or {}).get("overlay_name", "exl3-cooperative.py")
    checks = [
        f"test -d {shlex.quote(stage)}",
        f"cd {shlex.quote(stage)}",
        f"printf '%s  cooperative_moe.so\\n' {shlex.quote(artifact['sha256'])} | sha256sum -c -",
    ]
    runtime_sha = runtime.get("sha256", "")
    if runtime_sha:
        checks.append(
            f"printf '%s  runtime.py\\n' {shlex.quote(runtime_sha)} | sha256sum -c -"
        )
    checks += [f"test -s {shlex.quote(overlay)}", "sha256sum -c SHA256SUMS"]
    return "set -euo pipefail; " + "; ".join(checks)


def _runtime_stage_valid(cfg, idx, artifact):
    stage, _, _, _ = _runtime_artifact_dirs(cfg, artifact)
    result = ssh(
        cfg,
        idx,
        _runtime_stage_verify_command(artifact, stage),
        check=False,
        capture=True,
    )
    return result.returncode == 0


def _runtime_manifest_payload(artifact):
    keep = {
        "id": artifact.get("id"),
        "kind": artifact.get("kind"),
        "name": artifact.get("name"),
        "sha256": artifact.get("sha256"),
        "runtime_py": artifact.get("runtime_py"),
        "source": artifact.get("source"),
        "pins": artifact.get("pins"),
        "image": artifact.get("image"),
    }
    return base64.b64encode(
        (json.dumps(keep, sort_keys=True, indent=2) + "\n").encode()
    ).decode()


def _fetch_runtime_artifact_on_head(cfg, head, artifact):
    source = artifact.get("source") or {}
    if source.get("type") != "git":
        raise SystemExit(f"Unsupported runtime artifact source: {source}")
    repo = str(source.get("repo", ""))
    ref = str(source.get("ref", ""))
    if not repo.startswith("https://github.com/") or not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise SystemExit(f"Runtime artifact requires an exact GitHub commit pin: {source}")
    stage, container, _, _ = _runtime_artifact_dirs(cfg, artifact)
    overlay = (artifact.get("stage") or {}).get("overlay_name", "exl3-cooperative.py")
    binary_path = source["binary_path"]
    runtime_path = source["runtime_path"]
    generator_path = source["generator_path"]
    stock_path = source["stock_path"]
    gate_path = source["gate_path"]
    support_path = source["gate_support_path"]
    provenance_path = source.get("provenance_path", "")
    manifest64 = _runtime_manifest_payload(artifact)
    runtime_sha = (artifact.get("runtime_py") or {}).get("sha256", "")
    files = [
        "cooperative_moe.so",
        "runtime.py",
        overlay,
        "prepare_profile.py",
        "test_cuda_integration.py",
        "test_exl3_overlay.py",
    ]
    if provenance_path:
        files.append("cooperative_moe-build.log")
    sums = " ".join(shlex.quote(item) for item in files)
    provenance_install = (
        f'install -m 0644 "$CHECKOUT/{provenance_path}" "$TMP/cooperative_moe-build.log"; '
        if provenance_path
        else ""
    )
    script = f'''set -euo pipefail
STAGE={shlex.quote(stage)}
TMP="${{STAGE}}.tmp.$$"
CHECKOUT="$(mktemp -d /tmp/dgx-runtime-source.XXXXXX)"
cleanup() {{ rm -rf "$TMP" "$CHECKOUT"; }}
trap cleanup EXIT
rm -rf "$TMP"; mkdir -p "$TMP"
git init -q "$CHECKOUT"
git -C "$CHECKOUT" remote add origin {shlex.quote(repo)}
git -C "$CHECKOUT" fetch -q --depth=1 origin {shlex.quote(ref)}
git -C "$CHECKOUT" checkout -q --detach FETCH_HEAD
test "$(git -C "$CHECKOUT" rev-parse HEAD)" = {shlex.quote(ref)}
install -m 0644 "$CHECKOUT/{binary_path}" "$TMP/cooperative_moe.so"
install -m 0644 "$CHECKOUT/{runtime_path}" "$TMP/runtime.py"
printf '%s  cooperative_moe.so\n' {shlex.quote(artifact['sha256'])} | (cd "$TMP" && sha256sum -c -)
printf '%s  runtime.py\n' {shlex.quote(runtime_sha)} | (cd "$TMP" && sha256sum -c -)
python3 "$CHECKOUT/{generator_path}" --stock "$CHECKOUT/{stock_path}" --artifacts "$TMP" --runtime-directory {shlex.quote(container)} --output "$TMP/{overlay}"
install -m 0644 "$CHECKOUT/{generator_path}" "$TMP/prepare_profile.py"
install -m 0644 "$CHECKOUT/{gate_path}" "$TMP/test_cuda_integration.py"
install -m 0644 "$CHECKOUT/{support_path}" "$TMP/test_exl3_overlay.py"
{provenance_install}printf '%s' {shlex.quote(manifest64)} | base64 -d > "$TMP/manifest.json"
(cd "$TMP" && sha256sum {sums} > SHA256SUMS && sha256sum -c SHA256SUMS)
rm -rf "$STAGE"; mkdir -p "$(dirname "$STAGE")"; mv "$TMP" "$STAGE"
trap - EXIT; rm -rf "$CHECKOUT"
'''
    print(f"\n== runtime fetch on node{head}: {artifact['id']} @ {ref[:12]} ==")
    result = ssh(cfg, head, script, check=False)
    if result.returncode:
        raise SystemExit(f"Runtime artifact fetch/stage failed on node{head}: {artifact['id']}")
    if not _runtime_stage_valid(cfg, head, artifact):
        raise SystemExit(f"node{head}: staged runtime artifact failed post-fetch verification")


def _sync_runtime_artifact(cfg, head, dst, artifact):
    stage, _, _, _ = _runtime_artifact_dirs(cfg, artifact)
    dest = f"{ssh_user(cfg)}@{con_name(cfg, dst)}"
    command = (
        "set -euo pipefail; "
        f"ssh -o BatchMode=yes -o ConnectTimeout=7 {shlex.quote(dest)} "
        f"'rm -rf {shlex.quote(stage)} && mkdir -p {shlex.quote(stage)}'; "
        f"rsync -aH --partial --info=progress2 {shlex.quote(stage.rstrip('/') + '/')} "
        f"{shlex.quote(dest + ':' + stage.rstrip('/') + '/')}"
    )
    print(f"\n== runtime artifact sync node{head} -> node{dst} over {con_name(cfg, dst)} ==")
    result = ssh(cfg, head, command, check=False)
    if result.returncode or not _runtime_stage_valid(cfg, dst, artifact):
        raise SystemExit(f"Runtime artifact sync/verification failed node{head} -> node{dst}")


def _runtime_image_verify_command(image, *, pull):
    image_ref = str(image.get("reference", "")).strip()
    digest = str(image.get("digest", "")).strip()
    legacy_config_id = str(image.get("legacy_config_id", "")).strip()
    if not image_ref or not digest or not image_ref.endswith("@" + digest):
        raise SystemExit("Runtime artifact image metadata must use an immutable reference@digest")
    pull_cmd = (
        'if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then docker pull "$IMAGE"; fi'
        if pull
        else 'docker image inspect "$IMAGE" >/dev/null 2>&1'
    )
    legacy_check = (
        f' && [ "$ACTUAL_ID" != {shlex.quote(legacy_config_id)} ]'
        if legacy_config_id
        else ""
    )
    return f'''IMAGE={shlex.quote(image_ref)}
EXPECTED_DIGEST={shlex.quote(digest)}
{pull_cmd}
ACTUAL_ID="$(docker image inspect -f '{{{{.Id}}}}' "$IMAGE")"
if [ "$ACTUAL_ID" != "$EXPECTED_DIGEST" ]{legacy_check}; then
  echo "unexpected image identity: $ACTUAL_ID (expected target digest $EXPECTED_DIGEST or legacy config id {legacy_config_id or '<none>'})" >&2
  exit 43
fi'''


def _runtime_gate_command(artifact, stage, host_mount, container_mount):
    image = artifact.get("image") or {}
    gate = artifact.get("gate") or {}
    checks = int(gate.get("checks", 0))
    overlay = (artifact.get("stage") or {}).get("overlay_name", "exl3-cooperative.py")
    if checks < 1:
        raise SystemExit(f"Runtime artifact gate metadata is incomplete: {artifact.get('id')}")
    image_verify = _runtime_image_verify_command(image, pull=True)
    conflict = "; ".join(
        f"if docker ps --format '{{{{.Names}}}}' | grep -Fxq {shlex.quote(name)}; "
        f"then echo 'ERROR: stop {name} before runtime gate' >&2; exit 42; fi"
        for name in gate.get("conflicting_containers", [])
    )
    parser = (
        "import json,sys; final=None\n"
        "for line in open(sys.argv[1], errors='replace'):\n"
        " try: obj=json.loads(line)\n"
        " except Exception: continue\n"
        " if obj.get('stage')=='complete': final=obj\n"
        "expected=int(sys.argv[2])\n"
        "assert final and final.get('status')=='pass' and int(final.get('checks',-1))==expected, final"
    )
    log = f"{stage}/gate.log"
    return f'''set -euo pipefail
{_runtime_stage_verify_command(artifact, stage)}
{conflict}
{image_verify}
set +e
docker run --rm --network none --gpus all --cpus 2 --memory 6g --memory-swap 6g \\
  -e DSV41_COOP_MAINTENANCE_TEST=1 -e MAX_JOBS=2 -e OMP_NUM_THREADS=1 \\
  -e OPENBLAS_NUM_THREADS=1 -e PYTHONDONTWRITEBYTECODE=1 \\
  -v {shlex.quote(host_mount + ':' + container_mount)} \\
  -v {shlex.quote(stage + '/' + overlay + ':/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/quantization/exl3.py:ro')} \\
  -v {shlex.quote(stage + '/test_exl3_overlay.py:/opt/dsv41/test_exl3_overlay.py:ro')} \\
  -v {shlex.quote(stage + '/test_cuda_integration.py:/opt/dsv41/test_cuda_integration.py:ro')} \\
  --entrypoint python3 "$IMAGE" /opt/dsv41/test_cuda_integration.py 2>&1 | tee {shlex.quote(log)}
RC=${{PIPESTATUS[0]}}
set -e
test "$RC" -eq 0
python3 -c {shlex.quote(parser)} {shlex.quote(log)} {checks}
'''


def _run_runtime_gate(cfg, idx, artifact):
    stage, _, host_mount, container_mount = _runtime_artifact_dirs(cfg, artifact)
    print(f"\n== runtime GPU gate node{idx}: {artifact['id']} ==")
    result = ssh(
        cfg,
        idx,
        _runtime_gate_command(artifact, stage, host_mount, container_mount),
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"Runtime GPU gate failed on node{idx}: {artifact['id']}")
    image = artifact["image"]
    observed = ssh(
        cfg,
        idx,
        f"docker image inspect -f '{{{{.Id}}}}' {shlex.quote(image['reference'])}",
        capture=True,
    ).stdout.strip()
    gate_doc = {
        "version": 1,
        "node_index": int(idx),
        "artifact_id": artifact["id"],
        "artifact_sha256": artifact["sha256"],
        "image_reference": image["reference"],
        "image_digest": image["digest"],
        "image_legacy_config_id": image.get("legacy_config_id", ""),
        "observed_image_id": observed,
        "checks": int((artifact.get("gate") or {}).get("checks", 0)),
        "status": "pass",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = base64.b64encode(
        (json.dumps(gate_doc, sort_keys=True, indent=2) + "\n").encode()
    ).decode()
    ssh(
        cfg,
        idx,
        f"printf '%s' {shlex.quote(payload)} | base64 -d > {shlex.quote(stage + '/gate.json')}",
    )


def _runtime_gate_valid(cfg, idx, artifact):
    stage, _, _, _ = _runtime_artifact_dirs(cfg, artifact)
    image = artifact.get("image") or {}
    expected = {
        "artifact_sha256": artifact["sha256"],
        "image_reference": image.get("reference"),
        "image_digest": image.get("digest"),
        "image_legacy_config_id": image.get("legacy_config_id", ""),
        "checks": int((artifact.get("gate") or {}).get("checks", 0)),
        "status": "pass",
        "node_index": int(idx),
    }
    expected64 = base64.b64encode(json.dumps(expected, sort_keys=True).encode()).decode()
    parser = (
        "import base64,json,sys; a=json.load(open(sys.argv[1])); "
        "e=json.loads(base64.b64decode(sys.argv[2])); "
        "assert all(a.get(k)==v for k,v in e.items()), (a,e)"
    )
    verify = _runtime_stage_verify_command(artifact, stage)
    image_verify = _runtime_image_verify_command(image, pull=False)
    cmd = (
        f"{verify}; python3 -c {shlex.quote(parser)} {shlex.quote(stage + '/gate.json')} {shlex.quote(expected64)}; "
        f"{image_verify}"
    )
    return ssh(cfg, idx, cmd, check=False, capture=True).returncode == 0


def _runtime_activation_env(cfg, artifact):
    stage, _, _, _ = _runtime_artifact_dirs(cfg, artifact)
    activation = artifact.get("activation") or {}
    env = {str(k): str(v) for k, v in (activation.get("env") or {}).items()}
    overlay_env = str(activation.get("overlay_env", "")).strip()
    if overlay_env:
        overlay = (artifact.get("stage") or {}).get("overlay_name", "exl3-cooperative.py")
        env[overlay_env] = posixpath.join(stage, overlay)
    return env


def _recipe_runtime_artifacts_ready(cfg, cluster_id, t, recipe, *, required):
    if not recipe.get("runtime_artifact_provider"):
        return [], {}
    manifest = _runtime_artifact_provider_manifest(recipe)
    artifacts = [dict(item) for item in manifest["runtime_artifacts"]]
    state = _load_runtime_state(cluster_id, t["name"], recipe["id"])
    valid_state = bool(
        state
        and state.get("version") == 1
        and state.get("cluster") == cluster_id
        and state.get("topology") == t["name"]
        and state.get("recipe") == recipe["id"]
        and state.get("nodes") == [int(x) for x in t["nodes"]]
        and state.get("gate_status") == "pass"
    )
    remote_ok = valid_state
    if remote_ok:
        for artifact in artifacts:
            for idx in t["nodes"]:
                if not _runtime_gate_valid(cfg, idx, artifact):
                    remote_ok = False
                    break
            if not remote_ok:
                break
    if not remote_ok:
        if required:
            raise SystemExit(
                "Recipe runtime prerequisites are not staged and GPU-gated on every topology node. "
                f"Run: ./scripts/25-runtime-reconcile.sh {cluster_id} {t['name']} "
                f"--recipe {recipe['id']} --fetch --apply --gate"
            )
        return artifacts, {}
    env = {}
    for artifact in artifacts:
        env.update(_runtime_activation_env(cfg, artifact))
    return artifacts, env


def cmd_runtime_reconcile(args):
    cfg, _ = load_cfg(args.cluster)
    t = resolve_topology_arg(cfg, args.cluster, args.topology)
    records = selected_runtime_recipe_records(args.recipe)
    ensure_recipe_sources(records)
    head = int(t["nodes"][0])
    for _, recipe in records:
        if t["tp"] not in [int(x) for x in recipe.get("supported_tp", [])]:
            raise SystemExit(f"Recipe {recipe['id']} does not support tp={t['tp']}")
        manifest = _runtime_artifact_provider_manifest(recipe)
        artifacts = [dict(item) for item in manifest["runtime_artifacts"]]
        print(f"\nRUNTIME RECIPE: {recipe['id']} topology={t['name']} nodes={t['nodes']}")
        for artifact in artifacts:
            stage, container, _, _ = _runtime_artifact_dirs(cfg, artifact)
            print(f"  {artifact['id']}: sha256={artifact['sha256']}")
            print(f"    host:      {stage}")
            print(f"    container: {container}")
            print(
                f"    source:    {(artifact.get('source') or {}).get('repo')}@"
                f"{(artifact.get('source') or {}).get('ref')}"
            )
            print(f"    image:     {(artifact.get('image') or {}).get('reference')}")
        if not (args.fetch or args.apply or args.gate):
            state = _load_runtime_state(args.cluster, t["name"], recipe["id"])
            print(
                "  state:",
                "qualified" if state and state.get("gate_status") == "pass" else "not qualified",
            )
            continue
        # Invalidate previous qualification before any mutation/requalification.
        # A failed fetch/apply/gate must never leave an older PASS usable by Job 30.
        _write_runtime_state(
            args.cluster,
            t["name"],
            recipe["id"],
            {
                "version": 1,
                "cluster": args.cluster,
                "topology": t["name"],
                "recipe": recipe["id"],
                "nodes": [int(x) for x in t["nodes"]],
                "artifacts": [],
                "gate_status": "pending",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if args.fetch:
            for artifact in artifacts:
                _fetch_runtime_artifact_on_head(cfg, head, artifact)
        if args.apply:
            for artifact in artifacts:
                if not _runtime_stage_valid(cfg, head, artifact):
                    raise SystemExit(
                        f"Head node{head} has no verified runtime artifact; rerun with --fetch --apply"
                    )
                stage, _, _, _ = _runtime_artifact_dirs(cfg, artifact)
                ssh(
                    cfg,
                    head,
                    f"rm -f {shlex.quote(stage + '/gate.json')} {shlex.quote(stage + '/gate.log')}",
                )
                for idx in t["nodes"]:
                    if int(idx) == head:
                        continue
                    _sync_runtime_artifact(cfg, head, int(idx), artifact)
        staged = {}
        for artifact in artifacts:
            staged[artifact["id"]] = [
                int(idx)
                for idx in t["nodes"]
                if _runtime_stage_valid(cfg, int(idx), artifact)
            ]
        gate_status = "pending"
        if args.gate:
            for artifact in artifacts:
                missing = [
                    int(idx)
                    for idx in t["nodes"]
                    if int(idx) not in staged[artifact["id"]]
                ]
                if missing:
                    raise SystemExit(
                        f"Runtime artifact {artifact['id']} is not staged on nodes {missing}; "
                        "use --fetch --apply first"
                    )
                stage, _, _, _ = _runtime_artifact_dirs(cfg, artifact)
                for idx in t["nodes"]:
                    ssh(
                        cfg,
                        int(idx),
                        f"rm -f {shlex.quote(stage + '/gate.json')} {shlex.quote(stage + '/gate.log')}",
                    )
                for idx in t["nodes"]:
                    _run_runtime_gate(cfg, int(idx), artifact)
            gate_status = "pass"
        state = {
            "version": 1,
            "cluster": args.cluster,
            "topology": t["name"],
            "recipe": recipe["id"],
            "nodes": [int(x) for x in t["nodes"]],
            "artifacts": [
                {
                    "id": artifact["id"],
                    "sha256": artifact["sha256"],
                    "staged_nodes": staged[artifact["id"]],
                }
                for artifact in artifacts
            ],
            "gate_status": gate_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_runtime_state(args.cluster, t["name"], recipe["id"], state)
        if gate_status == "pass":
            print(f"RUNTIME RECONCILE OK: {recipe['id']} qualified on nodes {t['nodes']}")
        else:
            print(f"RUNTIME RECONCILE STAGED: {recipe['id']} (GPU gate pending)")


def _git_head(path):
    r=run(["git","-C",str(path),"rev-parse","HEAD"],capture=True,check=False)
    value=(r.stdout or "").strip()
    if r.returncode or not re.fullmatch(r"[0-9a-fA-F]{40}",value):
        raise SystemExit(f"Cannot determine pinned source revision for {path}")
    return value.lower()


def _build_recipe_runtime_archive(recipe, profile_path, compat):
    """Package only tracked recipe source plus the small compatibility engine."""
    source=_recipe_source_path(recipe)
    source_rel=source.relative_to(ROOT)
    tracked=run(["git","-C",str(source),"ls-files"],capture=True,check=False)
    if tracked.returncode:
        raise SystemExit(f"Could not enumerate tracked files in pinned recipe source {source_rel}")
    fd,name=tempfile.mkstemp(prefix="dgx-recipe-runtime-",suffix=".tar.gz")
    os.close(fd)
    archive=Path(name)
    try:
        with tarfile.open(archive,"w:gz") as tf:
            tf.add(ROOT/"fleet.py",arcname="fleet.py",recursive=False)
            tf.add(ROOT/"scripts"/"smoke-openai.py",arcname="scripts/smoke-openai.py",recursive=False)
            tf.add(profile_path,arcname=f"profiles/{profile_path.name}",recursive=False)
            tf.add(compat,arcname="config/cluster.toml",recursive=False)
            for rel in tracked.stdout.splitlines():
                if not rel:
                    continue
                src=source/rel
                if src.exists() or src.is_symlink():
                    tf.add(src,arcname=str(source_rel/rel),recursive=False)
        return archive
    except Exception:
        archive.unlink(missing_ok=True)
        raise


def _run_profile_bridge_on_head(cfg, cluster_id, t, recipe, operation, selected_profile, profile_path, compat, runtime_env):
    source=_recipe_source_path(recipe)
    revision=_git_head(source)
    head=int(t["nodes"][0])
    user=ssh_user(cfg)
    raw=node(cfg,head)["management"]
    remote_root=posixpath.join(
        f"/home/{user}",".local","share","dgx-spark-fleet","recipe-runtime",
        cluster_id,t["name"],recipe["id"],revision[:12],
    )
    archive=_build_recipe_runtime_archive(recipe,profile_path,compat)
    remote_archive=f"/tmp/dgx-spark-fleet-{cluster_id}-{recipe['id']}-{revision[:12]}.tar.gz"
    try:
        print(f"Staging pinned recipe runtime on node{head}: {remote_root}")
        run(["scp","-q",str(archive),f"{user}@{raw}:{remote_archive}"])
        extract=(
            f"set -e; mkdir -p {shlex.quote(remote_root)}; "
            f"tar -xzf {shlex.quote(remote_archive)} -C {shlex.quote(remote_root)}; "
            f"rm -f {shlex.quote(remote_archive)}"
        )
        ssh(cfg,head,extract)
    finally:
        archive.unlink(missing_ok=True)
    env_args=" ".join(f"{k}={shlex.quote(str(v))}" for k,v in sorted(runtime_env.items()))
    command=(
        f"set -e; cd {shlex.quote(remote_root)}; "
        f"{env_args} python3 ./fleet.py --config ./config/cluster.toml "
        f"profile {shlex.quote(operation)} {shlex.quote(selected_profile)}"
    )
    print(f"Executing recipe lifecycle on topology head node{head} ({mng_name(cfg,head)})")
    r=ssh(cfg,head,command,check=False)
    if r.returncode:
        raise SystemExit(r.returncode)


def cmd_recipe(args):
    if args.action=="list":
        for _,r in recipe_files(): print(f"{r['id']:28} tp={r.get('supported_tp',[])}  {r.get('title','')}")
        return
    cfg,_=load_cfg(args.cluster); t=resolve_topology_arg(cfg,args.cluster,args.topology)
    recipe_path,recipe=recipe_by_id(args.recipe)
    if t["tp"] not in [int(x) for x in recipe.get("supported_tp",[])]:
        raise SystemExit(f"Recipe {args.recipe} does not support tp={t['tp']}; supported={recipe.get('supported_tp',[])}")
    if args.operation in ("prepare", "start"):
        require_storage_ready(cfg, args.cluster, t["nodes"])
    adapter=ROOT/recipe["adapter"]
    if not adapter.exists(): raise SystemExit(f"Missing recipe adapter {adapter}")
    selected_profile,profile_path,profile=_recipe_profile(recipe,t["tp"])

    # Job 30 never acquires weights. It requires Job 20 to have reconciled the
    # selected recipe artifacts, then runs the pinned lifecycle on the head Spark.
    records=[(recipe_path,recipe)]
    ensure_recipe_sources(records)
    artifacts=_recipe_artifacts_ready(cfg,args.cluster,t,recipe)
    runtime_env=_recipe_runtime_env(cfg,profile,artifacts)
    _, native_runtime_env=_recipe_runtime_artifacts_ready(
        cfg,args.cluster,t,recipe,required=args.operation in ("prepare","start")
    )
    runtime_env.update(native_runtime_env)
    compat=generate_legacy_config(cfg,args.cluster,t)

    print(f"RECIPE: {recipe['id']} topology={t['name']} tp={t['tp']} nodes={t['nodes']}")
    if recipe.get("adapter")=="recipes/profile-bridge.sh":
        _run_profile_bridge_on_head(
            cfg,args.cluster,t,recipe,args.operation,selected_profile,
            profile_path,compat,runtime_env,
        )
        return

    env=os.environ.copy()
    env.update(runtime_env)
    env.update({
        "DGX_CLUSTER_ID":args.cluster,
        "DGX_TOPOLOGY":t["name"],
        "DGX_TP":str(t["tp"]),
        "DGX_NODE_INDEXES":" ".join(map(str,t["nodes"])),
        "DGX_HEAD_MNG":mng_name(cfg,t["nodes"][0]),
        "DGX_HEAD_CON":con_name(cfg,t["nodes"][0]),
        "DGX_WORKER_MNG":" ".join(mng_name(cfg,i) for i in t["nodes"][1:]),
        "DGX_WORKER_CON":" ".join(con_name(cfg,i) for i in t["nodes"][1:]),
        "DGX_SSH_USER":ssh_user(cfg),
        "DGX_MODEL_ROOT":configured_model_root(cfg),
        "DGX_RECIPE_ID":recipe["id"],
        "DGX_FLEET_CONFIG":str(compat),
        "DGX_PROFILE":selected_profile,
    })
    rc=subprocess.run([str(adapter),args.operation],cwd=ROOT,env=env).returncode
    if rc: raise SystemExit(rc)

def build_parser():
    p=argparse.ArgumentParser(description="Simple DGX Spark fleet operator controller")
    sub=p.add_subparsers(dest="cmd",required=True)
    q=sub.add_parser("init"); q.add_argument("cluster"); q.add_argument("--user"); q.add_argument("--node",action="append",type=parse_node_arg,required=True)
    q.add_argument("--fabric-primary",default="192.168.100.0/24"); q.add_argument("--fabric-secondary",default="192.168.101.0/24"); q.add_argument("--model-root"); q.add_argument("--force",action="store_true"); q.set_defaults(func=cmd_init)
    q=sub.add_parser("hosts"); q.add_argument("--cluster",required=True); q.set_defaults(func=cmd_hosts)
    q=sub.add_parser("bootstrap-management"); q.add_argument("--cluster",required=True); q.set_defaults(func=cmd_bootstrap_management)
    q=sub.add_parser("bootstrap-storage"); q.add_argument("--cluster",required=True); q.set_defaults(func=cmd_bootstrap_storage)
    q=sub.add_parser("bootstrap-fabric"); q.add_argument("--cluster",required=True); q.set_defaults(func=cmd_bootstrap_fabric)
    q=sub.add_parser("validate"); q.add_argument("--cluster",required=True); q.add_argument("--stage",choices=["management","storage","fabric","full"],default="full"); q.add_argument("--topology"); q.set_defaults(func=cmd_validate)
    q=sub.add_parser("topology"); q.add_argument("action",choices=["list","set","current"]); q.add_argument("--cluster",required=True); q.add_argument("name",nargs="?"); q.add_argument("--name",dest="name_opt",help="topology name for 'set' (preferred over positional form)"); q.set_defaults(func=cmd_topology)
    q=sub.add_parser("model-sync"); q.add_argument("--cluster",required=True); q.add_argument("--topology"); q.add_argument("--source-node",type=int); q.add_argument("--path",required=True); q.set_defaults(func=cmd_model_sync)
    q=sub.add_parser("model-reconcile", help="derive model requirements from pinned recipes and reconcile cluster storage")
    q.add_argument("--cluster",required=True)
    q.add_argument("--recipe",action="append",default=[],help="recipe id; repeatable; default: all enabled recipes")
    q.add_argument("--apply",action="store_true",help="synchronize artifacts already present on at least one Spark")
    q.add_argument("--download-missing",action="store_true",help="permit Internet downloads on a Spark seed (requires --apply)")
    q.add_argument("--seed-node",type=int,help="override automatic download seed for missing artifacts")
    q.set_defaults(func=cmd_model_reconcile)
    q=sub.add_parser("runtime-reconcile", help="reconcile pinned native runtime artifacts and run their GPU qualification gates")
    q.add_argument("--cluster",required=True)
    q.add_argument("--topology",help="topology to qualify; defaults to the active topology")
    q.add_argument("--recipe",action="append",default=[],help="runtime-artifact recipe id; repeatable; default: all enabled recipes that declare runtime artifacts")
    q.add_argument("--fetch",action="store_true",help="fetch the exact external source pin on the topology head and stage the verified artifact there")
    q.add_argument("--apply",action="store_true",help="replicate the staged artifact from the head to every topology node over the fabric")
    q.add_argument("--gate",action="store_true",help="pull/verify the pinned image and run the GPU integration gate on every topology node")
    q.set_defaults(func=cmd_runtime_reconcile)
    q=sub.add_parser("recipe"); q.add_argument("action",choices=["list","run"]); q.add_argument("recipe",nargs="?"); q.add_argument("operation",nargs="?",choices=["prepare","start","stop","status","smoke"]); q.add_argument("--cluster"); q.add_argument("--topology"); q.set_defaults(func=cmd_recipe)
    return p


def main():
    a=build_parser().parse_args()
    if a.cmd=="topology" and a.action=="set" and not (getattr(a,"name_opt",None) or getattr(a,"name",None)): raise SystemExit("topology set requires NAME (use --name NAME)")
    if a.cmd=="recipe" and a.action=="run" and (not a.recipe or not a.operation or not a.cluster): raise SystemExit("recipe run requires RECIPE OPERATION --cluster CLUSTER")
    a.func(a)

if __name__=="__main__": main()
