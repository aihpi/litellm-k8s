# Plan: fork `swiss-ai/model-launch` (`sml`) for the HPI Slurm cluster

Goal: launch vLLM/sglang jobs on the HPI Slurm cluster with `sml` so they
register in **our** OpenTela head (k8s, behind the wstunnel path) and become
LiteLLM backends — instead of the hand-written `~/otela-worker.sbatch`.
This document is the full context for a fresh session working in the fork.

Findings below are from reading `swiss-ai/model-launch@main` on 2026-09-03
(last push that day). Re-verify line numbers; file paths are stable.

## 1. What already works (the thing we must not break)

Proven 2026-09-03 end to end: Slurm vLLM job → `wss://api.aisc.hpi.de/otela-<TOKEN>/`
→ Caddy → nginx-proxy → wstunnel sidecar in `litellm-proxy` → `otela-head:43905`
→ registered as service `llm` → LiteLLM model `slurm-test` answered a request.
Runbook: `docs/opentela-slurm.md` in `aihpi/litellm-k8s`.

Facts a fork must encode (all non-secret):

| Item | Value |
| --- | --- |
| Head peer ID (deterministic, `--seed 43905`) | `QmWBnedcUEawmdTQTXgyY6BAQFDMwiUDndmRqgGkRBY4Qr` |
| Bootstrap multiaddr as seen from a worker | `/ip4/127.0.0.1/tcp/<LOCAL_TUNNEL_PORT>/p2p/<HEAD_PEER_ID>` |
| Tunnel target (resolved by the server side) | `otela-head.litellm.svc.cluster.local:43905` |
| Tunnel endpoint | `wss://api.aisc.hpi.de:443`, `--http-upgrade-path-prefix "otela-$TOKEN"` |
| Token | plaintext file `~/otela-tunnel-token` on Slurm home (mode 600). **Never in git.** |
| Head HTTP API (from inside k8s only) | `http://otela-head.litellm.svc.cluster.local:8092` (`/v1/dnt/table`, `/v1/service/llm/v1/...`) |
| Binaries | OpenTela `v0.2.4` `opentela-amd64`; wstunnel `v10.7.1` |
| Slurm | account `aisc-staff`, partition `aisc-batch`, `--exclude=ga03` (aarch64 Grace node; everything we have is x86-64), nodes are **shared** (need `--gres=gpu:N`, ports must not be fixed) |
| Compute nodes | no `nvcc` (`/usr/local/cuda` absent → `VLLM_USE_FLASHINFER_SAMPLER=0` or load a CUDA module); `/scratch/$USER` not writable; login node `rx02` |
| Reference job that works | `~/otela-worker.sbatch` on rx02 (copy below) |

OpenTela behaviours that bit us, from its source (`src/internal/protocol/bootstrap.go`,
`host.go`, `key.go`, `src/entry/cmd/root.go`):

1. `--bootstrap.addr` is **appended** to `bootstrap.static`, whose default is three
   public eth-easl bootstrap servers. A worker without `--bootstrap.static=`
   joins their public mesh and gossips strangers into our head's table; the head
   then load-balances `llm` onto them. Must always be overridden.
2. Identity key: `--seed 0` = load `$CONFIG_DIR/keys/id` else generate **random**;
   non-zero seed = deterministic from seed. An existing key file wins over the
   seed. Default config dir is `$HOME/.config/opentela` — shared home means every
   replica/job would be one peer. Need per-rank `--config-dir` (or `OF_CONFIG_DIR`)
   and a per-job seed.
3. Every setting is also an env var: prefix `OF_`, dots → underscores
   (`viper.SetEnvPrefix("of")`, `AutomaticEnv`). So `OF_BOOTSTRAP_STATIC`,
   `OF_CONFIG_DIR`, `OF_SEED` work without CLI flags. Viper ignores **empty** env
   vars by default, so override the static list with our own multiaddr, not `""`.
4. PSK network isolation is commented out upstream; the tunnel token is the only gate.

Reference sbatch (works; the fork's rendered `head.sh` must be equivalent):

```bash
#!/bin/bash
#SBATCH --account=aisc-staff --partition=aisc-batch --exclude=ga03
#SBATCH --gres=gpu:1 --time=02:00:00 --cpus-per-task=8 --mem=48G --job-name=otela-worker
PEER_ID=QmWBnedcUEawmdTQTXgyY6BAQFDMwiUDndmRqgGkRBY4Qr
export PATH="$HOME/vllm-venv/bin:$PATH"
unset PYTHONPATH
export VLLM_USE_FLASHINFER_SAMPLER=0
TOKEN=$(cat ~/otela-tunnel-token)
PORT=$((20000 + SLURM_JOB_ID % 10000))
TUN=$((30000 + SLURM_JOB_ID % 10000))
~/bin/wstunnel client -L tcp://127.0.0.1:$TUN:otela-head.litellm.svc.cluster.local:43905 \
  --http-upgrade-path-prefix "otela-$TOKEN" wss://api.aisc.hpi.de:443 &
sleep 3
~/bin/otela start --bootstrap.static= \
  --bootstrap.addr /ip4/127.0.0.1/tcp/$TUN/p2p/$PEER_ID \
  --config-dir /tmp/otela-$SLURM_JOB_ID \
  --subprocess "vllm serve Qwen/Qwen3-0.6B --port $PORT" \
  --service.name llm --service.port $PORT --seed $SLURM_JOB_ID
```

## 2. What `sml` does today (relevant parts)

Package `src/swiss_ai_model_launch/`. `sml advanced` (`cli/main.py:_run_advanced`)
builds `LaunchArgs` → `launchers/framework.py` renders `master.sh` (sbatch body)
plus per-rank `head.sh`/`follower.sh`/`router.sh` embedded as heredocs →
`launchers/slurm_launcher.py` runs `sbatch` from `~/.sml`.

| Concern | Where | Today | For HPI |
| --- | --- | --- | --- |
| sbatch header | `launchers/launch_args.py:to_sbatch_args`, `launchers/utils.py:render_sbatch_header` | `--exclusive --nodes=N --account --partition --time --output/--error`; nothing else, no passthrough | **code**: add `--gres`, optional `--exclusive`, `--cpus-per-task`, `--mem`, generic `--sbatch-arg` passthrough (for `--exclude`/`--constraint`) |
| container | `framework.py:_render_replica_launches` | `srun --container-writable --container-mounts --environment=<EDF>` per rank; `--environment` `required=True` in `main.py` | **gate**: needs pyxis+enroot on HPI. No bare path exists |
| framework port | `launch_args.py` `FRAMEWORK_PORT=8080`, injected into framework args | fixed 8080 (CSCS nodes exclusive) | **code**: shared nodes → derive from `$SLURM_JOB_ID` or `--exclusive` only |
| otela wrap | `framework.py:_opentela_wrap*` | `$OPENTELA_BIN start --bootstrap.addr <X> --service.name llm --service.port 8080 --label … --subprocess "…"`; no `--bootstrap.static`, `--config-dir`, `--seed` | config via `OF_*` env (see §4) or **code** |
| bootstrap addr | `framework.py` `OPENTELA_BOOTSTRAP_ADDR(_DEV)` | CSCS IPs; flag `--opentela-bootstrap-addr` overrides | flag works; change default |
| otela binary | `framework.py:_render_arch_detection` | `export OPENTELA_BIN=/opentelabin/{prod,dev}/otela-<arch>`; TOML mounts capstor dir at `/opentelabin` | config: mount our dir with `prod/otela-amd64` |
| pre-launch hook | `framework.py:_shebang_and_setup` | `--pre-launch-cmds` inlined into every rank script before the otela/framework line, inside the container, `set -ex` | where wstunnel client + `OF_*` exports go |
| metrics | `framework.py:_render_vmagent`, capstor paths in `launch_args.py` | vmagent + DCGM → CSCS Prometheus | `--disable-metrics --disable-dcgm-exporter`; change defaults |
| telemetry | `launch_args.py` `TELEMETRY_ENDPOINT`, `framework.py:_render_telemetry` | POST to `sml-dev.swissai.svc.cscs.ch`, `curl -sf … \|\| true` | harmless; set default `None` |
| health panel | `cli/healthcheck/checker.py` `_HEALTH_CHECK_URL` | polls `api.swissai.svc.cscs.ch` with `SML_SWISSAI_RESEARCH_API_KEY` (required by `sml init`) | **code**: point at `https://api.aisc.hpi.de/v1/chat/completions`, rename key |
| in-job replica health | `assets/replica_health_checker.py` | localhost probes + `/v1/self` via `srun` | portable, keep |
| model name | `launchers/served_name.py` | prefixed with cluster username: `<user>/<vendor>/<model>` | LiteLLM `model` must be `hosted_vllm/<user>/<vendor>/<model>` |
| `--system` | FirecREST only | ignored by `--launcher slurm` | ignore |
| GPUs per node | docs say 4; `topology.py` does not validate | none | fine |

## 3. Phase 0 — gate check (on rx02, before forking)

```bash
srun --help 2>&1 | grep -c -- --environment
ls /etc/slurm/plugstack.conf.d/ 2>/dev/null; grep -ril pyxis /etc/slurm 2>/dev/null | head
which enroot; enroot version 2>/dev/null
sinfo -p aisc-batch -o '%N %f %G %c %m' | head
```

- pyxis + enroot present → continue with §4–§7.
- absent → §8 (bare mode) is the real work; decide then whether the fork is worth it
  versus the 25-line sbatch.

## 4. Phase 1 — fork, dev setup, decide the shape

1. Fork to `aihpi/model-launch`, clone, follow `docs/development.md` (uv, editable
   install, `make test` or `pytest tests/unit`). Confirm the unit suite is green
   before touching anything.
2. Keep upstream mergeable: default branch tracks upstream `main`; our changes in
   small commits on `hpi` that are each either "new option, default = upstream
   behaviour" or "HPI default constants". Avoid renames.
3. Guiding rule: everything cluster-specific becomes a **flag or config with the
   upstream value as default**; HPI values live in one place (`assets/envs/vllm_hpi.toml`
   + an example script under `examples/hpi/`), not scattered constants.

## 5. Phase 2 — code changes (small, each with its unit test)

Order matters: each step renders with `--output-script` and is diffed against the
reference sbatch in §1.

1. **sbatch header** (`launch_args.py`, `utils.py`, `main.py` argparse):
   - `--gres` (str, default `None` → omitted), `--cpus-per-task`, `--mem`.
   - `--no-exclusive` (default keeps `--exclusive`).
   - `--sbatch-arg X` repeatable passthrough appended verbatim (covers
     `--exclude=ga03`, `--constraint`, `--qos`).
   - Tests: `tests/unit/test_to_sbatch_args.py`, `test_render_sbatch_header.py`.
2. **Framework port** (`launch_args.py`, `framework.py`): make `FRAMEWORK_PORT`
   a `LaunchArgs` field. Default 8080. Add `--framework-port auto` → rendered as
   `$((20000 + SLURM_JOB_ID % 10000))` in the rank scripts and in
   `--service.port`. Check every consumer: `_opentela_wrap`, `_render_router`,
   `replica_health_checker.py` (probes `localhost:8080`), telemetry payload,
   `test_port_guardrail.py`.
3. **OpenTela identity + private mesh** (`framework.py:_opentela_wrap*`): emit
   `--bootstrap.static "<bootstrap_addr>"`, `--config-dir "$HOME/.sml/job-${SLURM_JOB_ID}/otela-rank-${SLURM_NODEID:-0}"`
   (the job dir already exists on shared FS; or `/tmp`), `--seed $((SLURM_JOB_ID * 100 + ${SLURM_NODEID:-0}))`.
   Upstream-safe because on CSCS the static list containing only their head is
   equivalent to today. Tests: `test_rank_script_content.py`.
4. **Tunnel** (`framework.py`, `launch_args.py`, `main.py`): new option group
   `--tunnel-url wss://…`, `--tunnel-token-file PATH`, `--tunnel-target host:port`,
   `--tunnel-local-port auto`. When set, `_shebang_and_setup` (or a new
   `_render_tunnel`) emits:

   ```bash
   TUN=$((30000 + SLURM_JOB_ID % 10000))
   "$WSTUNNEL_BIN" client -L tcp://127.0.0.1:$TUN:<target> \
     --http-upgrade-path-prefix "otela-$(cat <token-file>)" <url> &
   sleep 3
   ```

   and the bootstrap addr becomes `/ip4/127.0.0.1/tcp/$TUN/p2p/<peer id>` — so
   `--opentela-bootstrap-addr` should accept a bare peer ID when `--tunnel-url`
   is set. `WSTUNNEL_BIN` resolved next to `OPENTELA_BIN` in
   `_render_arch_detection` (`/opentelabin/prod/wstunnel-<arch>`). Start with
   `--pre-launch-cmds` doing the same thing by hand to prove it, then promote.
5. **Defaults/URLs**: `OPENTELA_BOOTSTRAP_ADDR` (our peer ID), `TELEMETRY_ENDPOINT = None`,
   `metrics_*` defaults off, `_HEALTH_CHECK_URL` → `https://api.aisc.hpi.de/v1/chat/completions`,
   init wizard key renamed (`_RENAMED_KEYS` mechanism exists in `init_wizard.py`).
   Health-check model id must be what LiteLLM knows (see §7), not the raw served name.
6. **Arch guard**: we have no arm64 binaries; `_render_arch_detection` should
   `exit 1` on aarch64 with a clear message (or users pass `--sbatch-arg --exclude=ga03`).

Don't do: multi-node changes, router changes, FirecREST, MCP. They're portable.

## 6. Phase 3 — HPI environment (config, no code)

1. **Container image**: build a squashfs with enroot from `vllm/vllm-openai:<tag>`
   (or `docker.io/vllm/vllm-openai`), store on a path readable by compute nodes.
   Check `docs/building-images.md` for their CI flow; we only need the enroot
   import step. Pick a tag matching vLLM ≥ 0.28 (what the venv test used).
2. **`assets/envs/vllm_hpi.toml`**: `image = <sqsh path>`, `mounts = [ "<dir with prod/otela-amd64 and prod/wstunnel-amd64>:/opentelabin", "$HOME"?, HF cache dir ]`,
   `[env]` with `HF_HOME`, `VLLM_USE_FLASHINFER_SAMPLER=0` (unless the image has nvcc),
   `OF_BOOTSTRAP_STATIC=<our multiaddr>` as belt-and-braces. Drop the Slingshot
   `NCCL_*`/`FI_*` block and the `com.hooks.*` annotations (CSCS-specific).
3. **Binaries dir** on shared storage: `otela-share/prod/otela-amd64` (v0.2.4),
   `otela-share/prod/wstunnel-amd64` (v10.7.1). Symlink layout mirrors upstream.
4. **Token**: stays at `~/otela-tunnel-token`; the job reads it, never a flag value
   (would land in `squeue`/logs/telemetry labels).
5. **`examples/hpi/qwen3-0.6b-vllm.sh`**: the `sml advanced …` invocation equivalent
   to the reference sbatch.

## 7. Phase 4 — verify, then register

1. `sml advanced … --output-script /tmp/check`; read `master.sh` + `head.sh`; confirm:
   `#SBATCH --gres`, no `--exclusive` (or intended), wstunnel line before otela,
   `--bootstrap.static`, `--config-dir`, `--seed`, `--service.port` matching the
   framework `--port`, no capstor paths, no CSCS URLs.
2. Submit. In the job log expect: wstunnel connected → `bootstrap_connected=true` →
   relay reservation on `QmWBnedcUEaw` **only** → framework up → health check passed.
3. From the deploy host (lx04):

   ```bash
   kubectl -n litellm run curl-$RANDOM --rm -i --restart=Never --image=curlimages/curl:8.10.1 -- \
     -s http://otela-head.litellm.svc:8092/v1/dnt/table | grep -o '"id":"[^"]*"\|"identity_group":\[[^]]*\]'
   ```

   Exactly the head + our replicas. Any foreign node → stop, fix §5.3, restart head.
4. Register in LiteLLM via `POST /model/new` (not config.yaml — see
   `docs/opentela-slurm.md` §7): `model: hosted_vllm/<user>/<vendor>/<model>` (the
   sml-namespaced served name), `api_base: http://otela-head.litellm.svc.cluster.local:8092/v1/service/llm/v1`,
   `access_groups: ["otela-test"]` while testing. One LiteLLM row per served name;
   replicas are balanced by OpenTela, not by LiteLLM.
5. Point `_HEALTH_CHECK_URL` polling at that LiteLLM model name; a LiteLLM key with
   the access group is the "research API key".

## 8. Fallback if there is no pyxis: bare mode

Add `--container none`: `_render_replica_launches` emits
`srun --nodes=1 --ntasks=1 --nodelist=… bash "$RANKS_DIR/head.sh"` without
`--container-*`/`--environment`; `--environment` becomes optional; rank scripts get
the venv/PATH setup from `--pre-launch-cmds` (or a `--venv PATH` flag rendering
`export PATH=<venv>/bin:$PATH; unset PYTHONPATH`). Everything else in §5 still
applies. Multi-node Ray/sglang wiring should still work since it is plain shell.
Estimate the diff before starting; if it grows past a couple hundred lines, keep
the sbatch.

## 9. Success criteria

- `pytest tests/unit` green in the fork, including new tests for every new flag.
- One `sml advanced` command reproduces the §1 job on HPI and appears in LiteLLM.
- `--replicas 2` yields two distinct peer IDs in the head's table (identity fix).
- Head table never shows a node we didn't start (private mesh).
- Nothing secret in the fork: token only via file path; no LiteLLM keys.

## Non-goals for the first pass

Persisting past Slurm time limits (use k8s), metrics pipeline, FirecREST, MCP,
sglang router, arm64 support, upstreaming (later, once the flags settle).
