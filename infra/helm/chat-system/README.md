# chat-system (Helm chart)

Umbrella Helm chart that deploys the whole system: every application service,
the 4 extra vLLM models, and the supporting infra (Postgres, Redis, Milvus,
NFS). This is the Kubernetes expression of `/shared/CONTRACTS.md` and the
decisions in the project plan — the authoritative source for *why* a
component exists is the plan, this chart is just *how* it gets deployed.

## Value files

- `values.yaml` — central defaults, one top-level key per component
  (`gateway`, `client`, `ingestionService`, `retrievalOrchestrator`,
  `structuredDataAgent`, `summarizationService`, `celeryWorkers`, `vllm`,
  `redis`, `postgres`, `milvus`, `nfs`, `retentionSweeper`,
  `networkPolicies`).
- `values-dev.yaml` — no GPUs, no ingress, no NetworkPolicies, `MOCK_LLM=true`,
  `vllm.enabled=false`. For a cluster without GPU nodes or a local kind/minikube.
- `values-prod.yaml` — externally-managed Postgres (this chart's own Postgres
  StatefulSet is documented as dev/test-only), real ingress + TLS, HPAs tuned up.

## Templates, by directory

| Directory | What it deploys |
|---|---|
| `gateway/` | Deployment + Service + Ingress (NestJS BFF) |
| `client/` | Deployment + Service + Ingress (Angular, served statically) — on its own host so it doesn't collide with the gateway's Ingress rules |
| `ingestion-service/`, `retrieval-orchestrator/` | Deployment + Service + HPA (CPU-based by default; a KEDA/prometheus-adapter opt-in for queue-depth or latency-based scaling is documented but not wired up) |
| `structured-data-agent/` | Deployment + Service, with `runtimeClassName: gvisor` (configurable/disableable via `values.yaml` — requires gVisor or Kata installed on the cluster; see the service's own README for why) |
| `summarization-service/` | Deployment + Service |
| `celery-workers/` | Two **separate** Deployments — `celery-worker-ingestion` and `celery-worker-summarization` — fixed replica counts, no HPA. Kept separate deliberately: see both services' READMEs for the shared-Redis queue-collision bug this avoids. |
| `redis/`, `postgres/` | StatefulSet + PVC each. Postgres is explicitly dev/test-only in `values-prod.yaml`'s comments — point at an externally-managed instance for a real deployment. |
| `milvus/` | Standalone trio (milvus + etcd + minio-compatible object storage) as StatefulSets, matching `docker-compose.yml`'s local-dev topology. Swap for the Milvus Operator / cluster mode in production. |
| `nfs/` | PV + PVC, `ReadWriteMany`, configurable `storageClassName` (expects an `nfs-subdir-external-provisioner` or equivalent CSI driver already installed). This is the one volume every file-touching service (gateway's upload staging, ingestion-service, structured-data-agent, summarization-service) shares — **they must all mount it at the same absolute path**, which is exactly the class of bug documented in `ingestion-service/README.md`'s "known environment gotcha" (that gotcha is a *local-dev-only* problem precisely because this PVC prevents it in any real cluster). |
| `vllm/` | 4 Deployments for the small support models (embeddings, reranker, router, vision/OCR) via a shared `_vllm.tpl` named template. Each has the exact `vllm serve` args decided in the plan, `nvidia.com/gpu: 1`, and mounts a shared pre-populated HF-cache PVC + `HF_HUB_OFFLINE=1` (no internet access at runtime — weights must be staged into the PVC ahead of time as part of the air-gapped offline bundle process). **Does not touch** the existing production model deployments (muse30b/gemma3) — those are assumed to already exist and are referenced only by URL (`VLLM_GENERATION_URL`). |
| `cronjob/` | `retention-sweeper` — daily Job (schedule configurable) that's meant to purge conversations past their retention window (Milvus partition + NFS files + Postgres rows). The manifest + a `RETENTION_CHECK_QUERY` ConfigMap placeholder are here; **the sweeper script itself still needs to be written** — this directory only provides the scheduling/wiring. |
| `networkpolicy/` | Default-deny + allow-intra-namespace + an explicit allow-ingress-to-edge rule covering **both** `gateway` and `client` (the SPA has to be reachable by the browser directly, not just the API) — see the inline comment in `allow-ingress-to-edge.yaml`. |

## Air-gapped specifics

- Every `image:` is `registry.internal/...` with `imagePullPolicy:
  IfNotPresent` — nothing pulls from a public registry at deploy time.
- The expected workflow for getting images (including vLLM's model weights)
  into that registry is: build/pull with internet access elsewhere → `docker
  save` → tar → secure transfer into the air-gapped environment → `docker
  load` + push to the internal registry. `infra/offline-bundle/` is where
  the scripts for that process are meant to live (not yet written).

## Validating without a real cluster

Neither `kubectl` nor `kustomize` need to be available to sanity-check this
chart — `helm` alone is enough:

```bash
helm lint --strict infra/helm/chat-system
helm template infra/helm/chat-system                       # default values
helm template infra/helm/chat-system -f infra/helm/chat-system/values-dev.yaml
helm template infra/helm/chat-system -f infra/helm/chat-system/values-prod.yaml
```

All three currently render clean (0 lint failures, valid multi-document
YAML).

## Open decisions (not blocking, tracked in the project plan)

GPU allocation mechanism for the 4 small vLLM models (MIG vs. time-slicing
device-plugin), the real retention-sweeper script, which NFS
provisioner/CSI is actually approved in the target cluster, and the
image/model-weight approval process timeline for the internal registry.
