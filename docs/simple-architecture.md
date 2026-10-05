# Simplified fleet architecture

The design separates five layers:

1. **Physical cluster** — node indexes, management addresses, SSH user and model-root policy.
2. **Node bootstrap** — stable naming, all-node SSH mesh, passwordless sudo establishment, automatic Docker socket-group membership, and model-root ownership/writeability.
3. **Stable fabric** — automatic CX-7 discovery and deterministic `con-*` addresses.
4. **Topology group** — an ordered subset of physical nodes with TP size; groups may overlap.
5. **Recipe** — model/runtime-specific lifecycle adapter that declares supported TP sizes.

This means changing `tp4 -> prod2 + lab2 -> tp4` changes only topology selection and recipe lifecycle. It does not rewrite the physical inventory or network configuration.

Model data is treated separately from recipes. A model is acquired once on a chosen seed node and synchronized to a topology over the `con-*` fabric using rsync. This keeps model propagation independent of whether the serving recipe uses vLLM, SGLang, llama.cpp, ds4, or a future runtime.

Real `config/clusters/*.toml` files remain ignored. They contain infrastructure addresses but no credentials. SSH private keys remain node-local; only public keys are exchanged during management bootstrap.

The storage bootstrap writes `/etc/dgx-spark-fleet/node.json` on each node. The marker binds the physical cluster id, node index, SSH user and configured model root. Model synchronization and recipe `prepare`/`start` check this marker so a later inventory change cannot silently send hundreds of GiB to an unprepared location.
