# Slurm workers via OpenTela (test path)

Lets a vLLM process running as a Slurm job register as a backend behind the
LiteLLM proxy, without any inbound connectivity to the Slurm cluster. The
worker opens a WebSocket to the public LiteLLM host; a `wstunnel` sidecar in
the `litellm-proxy` pod turns that into a TCP stream to the OpenTela head
node's libp2p port; the worker then bootstraps into the head over that stream
and LiteLLM routes to it through the head's service proxy.

```
Slurm job ──wss://api.aisc.hpi.de/otela-<TOKEN>/──▶ Caddy ──▶ nginx-proxy:4100
  ──▶ litellm-service:8085 (wstunnel sidecar) ──TCP──▶ otela-head:43905
```

Kubernetes side is in this repo:

| Piece | Where |
| --- | --- |
| Head node Deployment + Service | `overlays/prod/otela-head/` |
| wstunnel sidecar on `litellm-proxy` | `overlays/prod/patches/litellm-otela-tunnel.yaml` |
| Port 8085 on `litellm-service` | `overlays/prod/patches/litellm-service-tunnel-port.yaml` |
| Routing of the secret path | `base/nginx/configmap.yaml` (the `location /otela-…/` block) |

The path prefix `otela-4f1c48a2bfa7f8c973952b60356f72e7` is the passcode. It
is plaintext in the nginx ConfigMap and the sidecar args, and it appears in
access logs — so it is obscurity bounded by `--restrict-to` (the tunnel can
only reach `otela-head:43905`), not a credential. To rotate it, change all
three: the nginx `location`, the sidecar `--restrict-http-upgrade-path-prefix`,
and the sbatch script below.

## 0. Outside this repo: Caddy

Caddy fronts nginx-proxy. Its `reverse_proxy` passes WebSocket upgrades by
default, but a tunnel session is one connection held for the life of the
job, so make sure no idle/stream timeout on that route is shorter than the
`proxy_read_timeout 3600s` nginx applies. If step 4 fails with a clean 404,
nginx never saw the path — that is Caddy.

## 1. Deploy and get the peer ID

```bash
cd ~/k8-deployments/litellm-k8s && git pull && kubectl apply -k overlays/prod
kubectl -n litellm rollout status deploy/otela-head
kubectl -n litellm rollout restart deploy/litellm-proxy   # picks up the sidecar
kubectl -n litellm rollout restart deploy/nginx-proxy     # subPath ConfigMap: needs a restart
```

The head's peer ID is deterministic (`--seed 0`) and is logged on start:

```bash
kubectl -n litellm logs deploy/otela-head -c otela | grep -iE "peer|listen"
```

Read it from *this* container only. `otela peer-id` run anywhere else (a
different pod, your laptop) generates a fresh key and prints an ID the head
does not have.

Keep the `12D3KooW…` string; it goes into the sbatch script.

## 2. Check routing from anywhere on the internet

```bash
curl -i https://api.aisc.hpi.de/otela-4f1c48a2bfa7f8c973952b60356f72e7/
```

Want **400 Bad Request** from wstunnel (a plain GET is not a WebSocket
upgrade — that is the sidecar answering, which is the point). A LiteLLM-shaped
404 means nginx routed to the API instead: the `location` block is not live.
The pasted plan said 400 *or* 426; wstunnel's source returns 400 for both a
non-upgrade request and a prefix mismatch.

## 3. Binaries on shared storage (login node)

Release asset names differ from the docs — use these, both pinned:

```bash
mkdir -p ~/bin
wget https://github.com/eth-easl/opentela/releases/download/v0.2.4/opentela-amd64 -O ~/bin/otela
wget https://github.com/erebe/wstunnel/releases/download/v10.7.1/wstunnel_10.7.1_linux_amd64.tar.gz -O - | tar xz -C ~/bin wstunnel
chmod +x ~/bin/otela ~/bin/wstunnel
```

If compute nodes have no internet, also pre-download the model into
`HF_HOME` from the login node.

## 4. Worker job

```bash
#!/bin/bash
#SBATCH --gres=gpu:1 --time=02:00:00 --cpus-per-task=8 --mem=48G
module load cuda
export HF_HOME=/scratch/$USER/hf

# Local 43905 -> LiteLLM host over WSS -> sidecar -> otela-head:43905.
# The destination hostname is resolved by the wstunnel SERVER inside the
# cluster, which is why a cluster-internal DNS name works from a Slurm node.
~/bin/wstunnel client \
  -L tcp://127.0.0.1:43905:otela-head.litellm.svc.cluster.local:43905 \
  --http-upgrade-path-prefix otela-4f1c48a2bfa7f8c973952b60356f72e7 \
  wss://api.aisc.hpi.de:443 &
sleep 3

~/bin/otela start \
  --bootstrap.addr /ip4/127.0.0.1/tcp/43905/p2p/<PEER_ID> \
  --subprocess "vllm serve Qwen/Qwen3-0.6B --port 8080" \
  --service.name llm --service.port 8080 \
  --seed 1
```

In the job's stdout you want wstunnel report a connection, otela report the
bootstrap peer as connected, then vLLM come up.

## 5. Verify registration from inside the cluster

```bash
kubectl -n litellm run curl --rm -it --restart=Never --image=curlimages/curl:8.10.1 -- \
  curl -s http://otela-head.litellm.svc:8092/v1/dnt/table
```

A second node advertising service `llm` with `model=Qwen/Qwen3-0.6B` means the
whole chain works.

## 6. Register with LiteLLM

Via `/model/new`, **not** `config.yaml` — a name in both places becomes two
router deployments (see `docs/model-inventory.md`), and `set-model-costs.sh`
skips config-backed rows. `hosted_vllm/` rather than `openai/` so
`reasoning_effort` and friends are forwarded instead of 400ing. The access
group keeps it off the default team's model list while it is a test.

```bash
curl -s -X POST "$LITELLM_URL/model/new" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{
    "model_name": "slurm-test",
    "litellm_params": {
      "model": "hosted_vllm/Qwen/Qwen3-0.6B",
      "api_base": "http://otela-head.litellm.svc.cluster.local:8092/v1/service/llm/v1",
      "api_key": "test-token"
    },
    "model_info": {"access_groups": ["otela-test"]}
  }'
```

Then call `slurm-test` with a normal LiteLLM key.

## Where it breaks

- Step 2 fails → Caddy or the nginx `location`. Not the tunnel.
- Tunnel connects but otela never registers → wrong `<PEER_ID>`, or the head is
  not listening on 43905 (`kubectl logs` for `Listen Addr`).
- Registered but LiteLLM errors → the `hosted_vllm/...` model string does not
  match what the worker's vLLM reports in `/v1/models`.

## Teardown

Delete the DB model (`/model/delete` by id), `scancel` the job, and drop the
two `otela-head/` resources and the two patches from
`overlays/prod/kustomization.yaml`. The nginx `location` block can stay or go;
with the sidecar gone it just 502s.
