# DGX Spark DeepSeek Fleet

A thin orchestration repository for running several fast DeepSeek Vision recipes on the same DGX Spark fleet without forking or copying their implementation.

The design deliberately separates:

- **site configuration** — node names, SSH, static RoCE addresses, interface names;
- **deployment profiles** — model/runtime/topology selection;
- **upstream recipes** — pinned as Git submodules and left unmodified;
- **large state** — model weights, Docker images, caches, Engram stores and generated `.env` files stay outside Git.

Run this repository on a management host with SSH access to the DGX Sparks. The
management host may be outside the DGX cluster; fleetctl stages recipe runtime
work on the selected topology head and drives the remaining Sparks over SSH.

## Included deployment tracks

| profile | topology | upstream | notes |
|---|---:|---|---|
| `v4-vision-vllm-tp2` | 2 Sparks | tonyd2wild V4 Vision recipe | V4 Flash Vision Exp, vLLM, DSpark, TP2 |
| `v4-vision-vllm-tp4` | 4 Sparks | same | same model/runtime, TP4 |
| `v41-vision-exl3-vllm-tp2` | 2 Sparks | MiaAI-Lab EXL3 recipe | V4.1 Vision, EXL3 2.9 bpw, vLLM, TP2 |
| `v41-vision-sglang-tp4` | 4 Sparks | MiaAI-Lab native recipe | V4.1 Vision, SGLang TP4; intended four-Spark destination |

The submodules are pinned by this repository. `fleet.py sources update` deliberately does **not** silently bless new upstream code: review and commit the resulting gitlink changes yourself.

## Upstream pins used when this repository was generated

- `sources/v4-vision-vllm` → `43201d1c2f281d238b8fd65f0f7168118f83169c`
- `sources/v41-exl3-vllm` → `6f7d1590ad49a2b8995188e45d7b9db31e677452`
- `sources/v41-sglang` → `cad252b7d3000cf21d919764e70a6912b6d3b0b0`

These are upstream repositories with their own licenses. This repository does not copy their source into its own tree or relicense it.

## Management-host bootstrap and named clusters

The management host does not need to be a DGX Spark, but it does need a supported
repo-local Python environment. Bootstrap it once from a fresh clone:

```bash
./scripts/bootstrap.sh
```

This follows the same repo-local environment pattern as Control Engineering Suite:
it creates or reuses `./venv_py312`, installs the checkout and development
dependencies from `pyproject.toml`, and initializes the pinned recipe submodules.
The host must provide Python 3.12+ to create the venv. Override interpreter discovery
with `DGX_FLEET_BOOTSTRAP_PYTHON=/path/to/python3.12` when needed.

Use the shell launchers for normal operation; they always use the repo environment,
so activation is not required:

```bash
./fleetctl --help
./fleet --help
```

Direct `.py` invocation is also available after activation:

```bash
source venv_py312/bin/activate
./fleetctl.py --help
```

Physical clusters are managed as **named inventories**. Create one inventory per cluster instead of copying a single `cluster.toml` around:

```bash
./fleet cluster init lab-tp2
$EDITOR config/clusters/lab-tp2.toml
./fleet cluster use lab-tp2
```

Create another independently:

```bash
./fleet cluster init production-tp4
$EDITOR config/clusters/production-tp4.toml
```

Useful inventory commands:

```bash
./fleet cluster list
./fleet cluster current
./fleet cluster show lab-tp2
./fleet cluster validate lab-tp2
./fleet cluster clone lab-tp2 lab2-tp2
```

`cluster use NAME` selects the default cluster for subsequent commands. For a one-off operation, override it without changing the default:

```bash
./fleet.py --cluster production-tp4 network discover
./fleet.py --cluster lab-tp2 profile status v41-vision-exl3-vllm-tp2
```

Every machine-affecting command prints the selected cluster and inventory path before doing work. If more than one inventory exists and no active cluster is selected, `fleet.py` refuses to guess.

Start by discovering the actual ConnectX mapping on every node:

```bash
./fleet.py network discover
```

Do **not** guess `enp...`/`roce...` names from the examples. Put the interfaces shown as `Up` into the selected `config/clusters/<name>.toml`.

Render the netplan that would be installed:

```bash
./fleet.py network render
```

Only after reviewing it:

```bash
./fleet.py network apply --yes
./fleet.py network verify
./fleet.py bootstrap
```

`network apply` writes only `/etc/netplan/40-dgx-spark-roce.yaml`. It does not add a gateway or modify the configured management NIC. Remote sudo is intentionally `sudo -n`: if your account is not allowed passwordless netplan changes, use the rendered YAML manually rather than risk an SSH prompt half-way through a fleet operation.

`scripts/bootstrap-node.sh --install` can install the small Ubuntu/RDMA/NFS utility set on a node. It intentionally does **not** install or replace NVIDIA drivers, CUDA, Docker or the NVIDIA container runtime.

## Direct two-Spark addressing

For a direct cable, a simple first plane is sufficient:

```text
spark1  192.168.100.10/24
spark2  192.168.100.11/24
```

If `ibdev2netdev` shows a second logical interface behind the connected port as `Up`, a second plane can use `192.168.101.10/24` and `.11/24`. `config/topologies/direct2.example.toml` is only a reference; the authoritative settings live in `config/cluster.toml`.

## Four Sparks through a switch

The example four-node fabric uses:

```text
spark1  192.168.192.10/24
spark2  192.168.192.11/24
spark3  192.168.192.12/24
spark4  192.168.192.13/24
```

A second confirmed RoCE plane can use `192.168.193.0/24`. No default route belongs on either fabric.

For the MiaAI SGLang TP4 recipe, the current upstream `.env.tp4` contract assumes the same `FABRIC_IFACE` name on all four Sparks. `fleet.py` checks this and refuses to render a misleading configuration if the names differ.

## Profile workflow

See profiles:

```bash
./fleet.py profile list
./fleet.py profile show v41-vision-exl3-vllm-tp2
```

Render the upstream configuration without starting anything:

```bash
./fleet.py profile configure v41-vision-exl3-vllm-tp2
```

For MiaAI profiles this creates the upstream ignored `.env`/`.env.tp4` by taking the pinned example and applying the cluster-wide addresses/interface names plus the profile overrides.

First-time heavy preparation is explicit:

```bash
./fleet.py profile prepare v41-vision-exl3-vllm-tp2
```

For the EXL3 profile this stages the resumable model download. `./start.sh` still owns image distribution, NFS and launch.

For the SGLang TP4 profile, `prepare` calls the upstream lifecycle in order:

```text
doctor -> build -> download -> share -> pack
```

Then start:

```bash
./fleet.py profile start v41-vision-exl3-vllm-tp2
```

Switching profiles stops the currently recorded deployment before starting the target:

```bash
./fleet.py switch v4-vision-vllm-tp2
./fleet.py switch v41-vision-exl3-vllm-tp2

# When Sparks 3+4 are installed:
./fleet.py switch v41-vision-sglang-tp4 --prepare
```

`--prepare` is intentionally not implicit: model downloads/builds can be hundreds of GiB and should not happen just because you changed the selected backend.

Status and smoke test:

```bash
./fleet.py profile status v41-vision-exl3-vllm-tp2
./fleet.py profile smoke v41-vision-exl3-vllm-tp2
```

The smoke test uses the profile's OpenAI-compatible endpoint and served model name. If the upstream recipe enables authentication, add `--api-key ...`.

## V4 Vision adapter: why it is different

The pinned V4 Vision upstream launchers contain lab-specific rank IPs, NIC names and model mount paths. This repository does **not** patch the submodule. Instead it:

1. reads the pinned launcher;
2. writes a generated launcher under `.state/rendered/<profile>/launcher.sh`;
3. replaces the rank map, master address, interface/HCA values and configured port;
4. stages the upstream Patch 3/Patch 4 and generated Vision-port files to `/var/tmp` on every rank;
5. copies the generated launcher to every Spark;
6. starts workers in descending rank order, then rank 0.

That keeps the upstream checkout clean and makes the site-specific delta auditable.

There is one intentional limitation: the V4 upstream launcher's exact deployed Docker tag is a local-only image. `profile prepare v4-vision-...` checks for that image and stops with an actionable error if it is missing. Build/restore the image according to the pinned upstream recipe rather than silently substituting a different runtime. Model weights likewise remain an upstream/site responsibility; each node's `model_host` controls the host mount used by the rendered launcher.

## Multiple physical clusters and state isolation

The selected physical cluster is not just a filename shortcut. Runtime state is namespaced by cluster:

```text
.state/active-cluster
.state/clusters/lab-tp2/active-profile
.state/clusters/lab-tp2/netplan/...
.state/clusters/lab-tp2/rendered/...
.state/clusters/production-tp4/active-profile
...
```

This prevents a model active on one cluster from being mistaken for the active deployment on another. Upstream MiaAI recipes still use their own `.env` files inside the submodules, so `fleet.py` deliberately regenerates the selected cluster's `.env` immediately before start, stop, or status operations. That avoids stale cluster-B addresses being used while operating on cluster A.

Real cluster inventory files are ignored by Git by default (`config/clusters/*.toml`) because they may contain private hostnames, internal addresses and local SSH-key paths. The directory itself is retained in Git. If you intentionally want an inventory under version control, add it explicitly after considering that information exposure.

`./fleet.py init-config` remains only as a compatibility alias that creates `config/clusters/default.toml`; new setups should use `cluster init NAME`. The old explicit `--config PATH` mode also remains available for advanced/legacy use.

## What is static vs switched

Static fleet state:

- management/control addresses;
- RoCE interface names and IP addresses;
- SSH identity/user;
- which physical/logical CX-7 interfaces belong to each Spark.

Switched by profiles:

- V4 vs V4.1;
- vLLM vs SGLang;
- EXL3 vs native weights;
- TP2 vs TP4;
- upstream `.env` values that are genuinely recipe-specific;
- service/container lifecycle.

This is deliberate. Reconfiguring the network every time a model changes would add failure modes without improving inference.

## Unsloth Studio

The intended end state is one stable OpenAI-compatible URL on rank 0 (the profiles default to port `8888`) while the backend changes underneath it. Unsloth Studio can therefore remain configured as an external OpenAI/vLLM-compatible provider; the cluster profile changes do not require moving Studio's web-search/code-execution layer onto the Sparks.

Profile model names currently default to:

```text
v4-vision-vllm-*          deepseek-v4-flash-dspark
v41-vision-exl3-vllm-tp2 DeepSeek-v4.1-Flash-EXL3
v41-vision-sglang-tp4     deepseek-v4.1-flash
```

If you want one invariant model alias as well as one invariant port, change `served_model`/the corresponding `[env]` override in each profile after confirming that the upstream parser accepts the alias.

## Safety / recovery

- `network render` before `network apply`.
- Keep management traffic on a different NIC from RoCE.
- Profile switching never changes netplan.
- Generated recipe config is derived from checked-in profile TOML plus the selected ignored `config/clusters/<name>.toml`.
- Active profile, rendered netplan, and generated launchers are isolated under `.state/clusters/<name>/`.
- Upstream submodules remain pinned; updates are explicit.
- `.state/active-profile` is local controller state only. If a start fails, inspect upstream logs before treating the switch as complete.
- For the V4 launcher, missing patch/vision files or model mounts cause the upstream launcher to fail before serving rather than silently continuing.

## Tests

```bash
make test
```

The tests do not need GPUs or network access. They validate netplan generation, `.env` merge behavior and the V4 launcher renderer against a synthetic pinned-launcher fixture.
