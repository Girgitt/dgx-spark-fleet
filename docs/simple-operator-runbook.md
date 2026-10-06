# DGX Spark Fleet — Simple Operator Runbook

This is the normal operator path. It intentionally hides DGX Spark interface names and RDMA device names.

## Assumptions

- all Sparks are standard DGX Spark systems;
- every Spark already has working IPv4 management connectivity (10GbE, WireGuard, etc.);
- the account used by the management host can SSH to every Spark without a password; it must already have ordinary sudo rights, but passwordless sudo is established by Job 01;
- one physical QSFP port per Spark is attached to the cluster fabric; a standard attached port exposes exactly two `Up` CX-7 logical interfaces;
- Docker is installed and its daemon/socket is available on each Spark; Job 01 establishes the required supplemental-group membership automatically;
- all nodes use the same Unix account name;
- the repository contains no credentials.

Stable names are generated identically on every Spark:

```text
mng-node1.dgx-c1   management path
con-node1.dgx-c1   primary high-speed fabric address
con2-node1.dgx-c1  second logical rail on the same connected QSFP port
```

## Fresh two-node cluster

From the management/Rundeck host, optionally inspect discovered neighbors:

```bash
./scripts/00-management-discover.sh
```

If you know a MAC suffix, filter the list:

```bash
./scripts/00-management-discover.sh aa:bb:cc 11:22:33
```

Create the cluster from the two management addresses:

```bash
./fleetctl.py init dgx-c1 \
  --user admin \
  --node 1=spark-1 \
  --node 2=spark-2 \
  --fabric-primary 10.200.100.0/24 \
  --fabric-secondary 10.200.101.0/24 \
  --model-root /home/admin/gguf
```

This creates one ignored local file, `config/clusters/dgx-c1.toml`. No interface names, passwords or SSH keys are entered manually. The only storage policy is the common `model_root`; model files themselves are never stored in Git.

### Job 01 — management bootstrap

```bash
./scripts/01-management-bootstrap.sh dgx-c1
```

The step is idempotent. It:

1. verifies management-node -> Spark passwordless SSH;
2. establishes passwordless sudo for the configured user when needed (the first interactive run may prompt once per node);
3. discovers the group owning `/var/run/docker.sock`, adds the configured user to it when needed, and proves Docker access in a fresh login session;
4. installs the same managed `/etc/hosts` block on every Spark;
5. ensures each Spark has its own Ed25519 user keypair;
6. collects only the user public keys and installs their union into `authorized_keys` on every Spark;
7. reads each Spark's SSH **host public keys** over the already-trusted management connection and installs a managed `known_hosts` block on every Spark for the `mng-*`, `con-*`, `con2-*` aliases and their current IP addresses.

Private user keys and private SSH host keys never leave their owning Spark. Host trust is derived from the authenticated management connection rather than `ssh-keyscan`/TOFU, so Job 04 never requires an operator to accept host fingerprints manually. No manual `usermod` or re-login is required: group changes are verified through a fresh login before this job succeeds. If Docker itself is missing or its daemon/socket is unavailable, the job fails rather than masking that as a group problem.

### Job 02 — storage bootstrap

```bash
./scripts/02-storage-bootstrap.sh dgx-c1
```

On each Spark this idempotent step:

1. creates the configured `model_root` if necessary;
2. sets ownership of the root directory to the configured SSH user and its primary group;
3. sets the root directory mode to `0755`;
4. performs a real create/delete write probe as that user;
5. records the non-secret bootstrap contract in `/etc/dgx-spark-fleet/node.json`;
6. reports the filesystem and available capacity.

It does **not** recursively change ownership of existing model contents. If `ssh_user` or `model_root` changes later, model synchronization and recipe `prepare`/`start` refuse to run until Job 02 is rerun.

### Job 03 — fabric bootstrap

```bash
./scripts/03-fabric-bootstrap.sh dgx-c1
```

On each Spark this step:

1. reads `ibdev2netdev`;
2. expects exactly two `Up` logical CX-7 interfaces for the attached physical QSFP port;
3. assigns deterministic addresses;
4. writes `/etc/netplan/40-dgx-spark-fabric.yaml`;
5. records the discovered interface/RDMA mapping in `/etc/dgx-spark-fleet/fabric.env`;
6. runs `netplan generate` and `netplan apply`.

For the default two-node cluster the stable addresses are:

```text
node1: con-node1.dgx-c1  -> 10.200.100.10
       con2-node1.dgx-c1 -> 10.200.101.10
node2: con-node2.dgx-c1  -> 10.200.100.11
       con2-node2.dgx-c1 -> 10.200.101.11
```

There are no gateways on the fabric subnets.

### Job 04 — validate

```bash
./scripts/04-validate.sh dgx-c1 tp2
```

This verifies naming, passwordless sudo, the storage bootstrap marker and model-root writeability, NVIDIA/Docker/RDMA prerequisites, and all-to-all passwordless SSH over both management and primary fabric names.

### Select topology

```bash
./scripts/10-topology-select.sh dgx-c1 tp2
```

Show it:

```bash
./fleetctl.py topology current --cluster dgx-c1
```

### Job 20 — reconcile models from pinned recipes

The operator does **not** type model names, Hugging Face repository names, shard counts or model paths. Those are derived from the currently pinned version of each enabled recipe.

The default processes **all enabled recipes**:

```bash
./scripts/20-model-reconcile.sh dgx-c1
```

This is read-only with respect to Spark model storage. Before inspecting recipes it automatically initializes any missing Git submodules for the selected recipes at the commits pinned by this repository. That small source-code fetch happens on the management host with `GIT_LFS_SKIP_SMUDGE=1`; model weights are not downloaded there.

It then:

1. enumerates enabled recipe descriptors;
2. ensures their pinned upstream source submodules are initialized locally;
3. runs each recipe's tracked artifact provider against that pinned upstream source;
4. deduplicates shared model requirements across recipes;
5. performs one recursive read-only inventory of every Spark under the configured `model_root`;
6. records model-like directories and standalone GGUF files, including sizes, `config.json` identity fields, safetensor indexes/shard counts, embedded Hugging Face repository hints, and local-dir Hugging Face revision metadata where available;
7. classifies each recipe requirement as `complete`, `partial`, `candidate`, or `missing`, and separately lists model data that is `UNMATCHED`;
8. chooses only positively identified `complete` copies as automatic synchronization sources;
9. prints the copy/review/download plan without changing Spark model data.

Matching is deliberately conservative and **format-aware**. Each recipe artifact provider declares the representation it consumes (the currently registered vLLM/SGLang recipes use `safetensors-directory`). Standalone GGUF files or GGUF-only directories can therefore never satisfy, or become candidates for, a safetensors checkpoint merely because their names resemble the same base model. They remain visible as `UNMATCHED` inventory until a recipe explicitly declares a compatible GGUF artifact/bundle. Within a compatible format, exact canonical/legacy names, embedded repository identity, or matching Hugging Face local-dir metadata can prove identity. Similar names or matching shard structure alone are only `candidate` evidence. A candidate is never silently adopted or overwritten and blocks an Internet download for that artifact until a later inventory/provider improvement can identify it positively. This prevents a differently named existing 200--500 GiB checkpoint from being redownloaded merely because its path is unfamiliar.

Process only selected recipes by repeating `--recipe`:

```bash
./scripts/20-model-reconcile.sh dgx-c1 --recipe v41-exl3-vision

./scripts/20-model-reconcile.sh dgx-c1 \
  --recipe v41-exl3-vision \
  --recipe v41-native-sglang
```

`--recipe all` is equivalent to omitting `--recipe`. It may not be combined with explicit recipe names.

To synchronize artifacts that already exist on at least one Spark:

```bash
./scripts/20-model-reconcile.sh dgx-c1 --apply
```

Only positively identified artifacts are copied automatically. `candidate` destinations are left untouched to avoid duplicating a likely existing checkpoint. Bulk copies are initiated on the selected source Spark and connect to peers via `con-nodeN...`, so model bytes travel over the CX-7 fabric. The management host only orchestrates.

To explicitly allow missing artifacts to be fetched from the Internet:

```bash
./scripts/20-model-reconcile.sh dgx-c1 --apply --download-missing
```

A genuinely missing artifact is downloaded **once on a Spark seed**, never onto the management workstation. The reconciler prefers the most complete positively identified partial copy so interrupted downloads can resume; otherwise it uses the first physical node. `--seed-node N` is available only as an override. If a plausible `candidate` exists for that artifact, the Internet download is skipped and Job 20 reports a review blocker instead. After a permitted download, the completed artifact is propagated to the other Sparks over `con-*`.

Hugging Face authentication is optional. When the management/Rundeck process has `HF_TOKEN` in its environment, Job 20 forwards that token only to the selected Spark download process over SSH stdin and exposes it there only as a transient `HF_TOKEN` environment variable. The token is not written to cluster TOML, not placed in the SSH command line, not printed by fleet command tracing, and `hf auth login` is not used, so the fleet does not persist the credential on the Spark. If `HF_TOKEN` is absent, public downloads continue anonymously. For an interactive shell, avoid putting the token in shell history:

```bash
read -rsp 'Hugging Face token: ' HF_TOKEN; echo
export HF_TOKEN
./scripts/20-model-reconcile.sh dgx-c1 --apply --download-missing
unset HF_TOKEN
```

For Rundeck, provide `HF_TOKEN` from a secure/key-storage-backed environment option rather than repository configuration or job text. A read-only Hugging Face token is sufficient for public/gated model downloads the account is authorized to access.

Hugging Face downloads are placed under the deterministic canonical layout:

```text
<model_root>/hf/<organization>/<repository>/
```

For example:

```text
/home/admin/gguf/hf/deepseek-ai/DeepSeek-V4.1-Flash/
/home/admin/gguf/hf/Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw/
```

If a complete model is already present under a legacy directory name, Job 20 reuses it rather than downloading it again and creates a canonical symlink when `--apply` is used. Shared requirements are merged: for example, if one recipe requires only Engram shards from `deepseek-ai/DeepSeek-V4.1-Flash` while another requires the complete checkpoint, the complete checkpoint satisfies both requirements and is downloaded only once.

`20-model-sync.sh` remains only as a deprecated explicit-path compatibility interface; new Rundeck jobs must use `20-model-reconcile.sh`.

### Job 25 — reconcile and qualify native runtime artifacts

Job 25 is separate from model reconciliation. It handles optional native runtime
artifacts that must be pinned, copied to every rank and GPU-qualified before a
recipe may use them. The current implementation registers cooperative MoE for the
TP2 V4.1 EXL3 recipe as `v41-exl3-vision-coop`.

Stop the stock/coop EXL3 service before running the GPU gate, then execute:

```bash
./scripts/25-runtime-reconcile.sh dgx-c1 tp2 \
  --recipe v41-exl3-vision-coop \
  --fetch --apply --gate
```

The phases are deliberate:

1. `--fetch` fetches the exact external Git commit on the topology head only,
   verifies `cooperative_moe.so` and its matching `runtime.py`, and generates the
   cooperative overlay into a content-addressed node-local cache;
2. `--apply` removes any previous qualification marker and replicates the verified
   cache from the head to the other selected ranks over `con-*`;
3. `--gate` requires the service stopped, verifies/pulls the immutable container
   immutable image digest, accepts either Docker image-ID representation (containerd target digest or legacy config digest), and runs the 54-case CUDA integration gate
   independently on every rank.

Qualification state is recorded under:

```text
.state/simple/<cluster>/runtime/<topology>/<recipe>/state.json
```

and each Spark keeps its own `gate.json` beside the staged runtime artifact. Job
30 rechecks both the current pin and every remote gate/image before allowing the
coop recipe to prepare/start. Any new fetch/apply/gate invalidates the prior PASS
first, so a failed requalification cannot fall back to stale success state.

After Job 25 passes:

```bash
./scripts/30-recipe-run.sh dgx-c1 tp2 v41-exl3-vision-coop prepare
./scripts/30-recipe-run.sh dgx-c1 tp2 v41-exl3-vision-coop start
```

To return to the stock path:

```bash
./scripts/30-recipe-run.sh dgx-c1 tp2 v41-exl3-vision-coop stop
./scripts/30-recipe-run.sh dgx-c1 tp2 v41-exl3-vision start
```

### Job 30 — recipe lifecycle on the topology head

List registered recipes:

```bash
./fleetctl recipe list
```

Job 30 is **not** a model acquisition path. Before any lifecycle action it derives the selected recipe's pinned artifact manifest, inventories the selected topology nodes, and requires every model artifact to be positively identified as complete. If not, it stops and tells the operator to run Job 20.

Legacy upstream profile recipes are staged from the management checkout onto the first node of the selected topology and executed there. The management host only packages/tranfers the pinned recipe source and drives SSH; Docker/image preparation, upstream lifecycle scripts and local runtime state execute on the topology-head Spark. The head's staged runtime lives under:

```text
~/.local/share/dgx-spark-fleet/recipe-runtime/<cluster>/<topology>/<recipe>/<pinned-source-sha>/
```

Only tracked files from the pinned upstream recipe source are staged. Model weights are not copied from the management host. Canonical model paths produced by Job 20 are injected into the upstream recipe environment. Upstream download stages are disabled/skipped when Job 20 has already reconciled those artifacts.

For the current TP2 EXL3 recipe:

```bash
./scripts/30-recipe-run.sh dgx-c1 tp2 v41-exl3-vision prepare
./scripts/30-recipe-run.sh dgx-c1 tp2 v41-exl3-vision start
./scripts/30-recipe-run.sh dgx-c1 tp2 v41-exl3-vision status
./scripts/30-recipe-run.sh dgx-c1 tp2 v41-exl3-vision smoke
./scripts/30-recipe-run.sh dgx-c1 tp2 v41-exl3-vision stop
```

`prepare` stages/verifies runtime prerequisites; it does not re-download the reconciled EXL3 or native V4.1/Engram weights. The EXL3 local-replica profile uses the canonical Job-20 model directories on both Sparks and keeps `AUTO_DOWNLOAD=0`. Runtime coordination from the head to workers uses the already-validated SSH mesh and fabric configuration.

## Four-node production/test split

Create the physical fleet once:

```bash
./fleetctl.py init dgx-c1 \
  --user zbig \
  --node 1=10.10.0.101 \
  --node 2=10.10.0.102 \
  --node 3=10.10.0.103 \
  --node 4=10.10.0.104
```

The generated topology presets are:

```text
tp2    = nodes 1,2
tp3    = nodes 1,2,3
tp4    = nodes 1,2,3,4
prod2  = nodes 1,2
lab2   = nodes 3,4
```

A typical controlled upgrade is then:

```bash
# production degraded to two nodes
./scripts/10-topology-select.sh dgx-c1 prod2
./scripts/30-recipe-run.sh dgx-c1 prod2 CURRENT_RECIPE start

# Stage/reconcile the new recipe artifacts cluster-wide. If the model already
# exists on nodes 3/4, the reconciler automatically reuses that copy as seed.
./scripts/20-model-reconcile.sh dgx-c1 --recipe NEW_RECIPE
./scripts/20-model-reconcile.sh dgx-c1 --recipe NEW_RECIPE --apply --download-missing
./scripts/30-recipe-run.sh dgx-c1 lab2 NEW_RECIPE start

# after acceptance, no model-path operation is required: all physical nodes have
# already been reconciled from the same recipe requirement.

# stop test/prod2 as appropriate, then bring production back as TP4
./scripts/10-topology-select.sh dgx-c1 tp4
./scripts/30-recipe-run.sh dgx-c1 tp4 NEW_RECIPE start
```

Topology groups deliberately may overlap. Running two recipes concurrently on overlapping node groups is an operator error; Rundeck job definitions should use node-group mutexes or mutually exclusive execution groups.

## Rundeck job mapping

Recommended job steps:

```text
00 Discover management candidates     scripts/00-management-discover.sh
01 Bootstrap management + SSH mesh    scripts/01-management-bootstrap.sh
02 Bootstrap storage/permissions      scripts/02-storage-bootstrap.sh
03 Bootstrap CX-7 fabric              scripts/03-fabric-bootstrap.sh
04 Validate cluster/topology          scripts/04-validate.sh
10 Select topology                    scripts/10-topology-select.sh
20 Reconcile recipe models             scripts/20-model-reconcile.sh
30 Recipe lifecycle                   scripts/30-recipe-run.sh
```

The scripts use ordinary exit codes and stdout/stderr and are therefore suitable as Rundeck command steps. Job 20 may additionally receive an optional `HF_TOKEN` secret through the process environment; it must not be placed in cluster files, command arguments, or repository-managed job definitions.

## Adding a future DGX Spark recipe

Cluster bootstrap does not change when a new model/runtime appears.

A recipe is a tracked TOML descriptor in `recipes/` plus an adapter executable. For model management it also declares `source` and an `artifact_provider`; the provider inspects the pinned upstream source and emits the current model requirements, so model versions are not duplicated in fleet configuration. `fleetctl.py` exports a stable adapter contract:

```text
DGX_CLUSTER_ID
DGX_TOPOLOGY
DGX_TP
DGX_NODE_INDEXES
DGX_HEAD_MNG
DGX_HEAD_CON
DGX_WORKER_MNG
DGX_WORKER_CON
DGX_SSH_USER
DGX_MODEL_ROOT
DGX_FLEET_CONFIG
DGX_RECIPE_ID
```

The adapter implements:

```text
prepare | start | stop | status | smoke
```

and declares supported TP values. Therefore a new recipe can support TP2, TP3, TP4 or any subset without changing management bootstrap, naming, fabric configuration or model-copy jobs.

The existing `profile-bridge.sh` adapter lets recipes reuse the older `fleet.py` profiles while the high-level operator layer stays stable.

## Compatibility script names

The earlier simplified-operations draft used `02-fabric-bootstrap.sh` and `03-validate.sh`. Those names remain as compatibility wrappers, but new Rundeck jobs should use the canonical numbered sequence above.
