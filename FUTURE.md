# Future: Automatic Model Registration

This document outlines a path to avoid manual edits when adding new models, while
still keeping the deployment workflow simple.

## Goal

- Deploy a new model service and have it show up in LiteLLM without manually
  updating config or running ad-hoc commands.

## Short-Term (Script-Based Registrar)

Create a small "registrar" script or Kubernetes Job that:

1. Waits for a model Service (or KServe InferenceService) to become Ready.
2. Calls LiteLLM's `POST /model/new` API to register the model.

Inputs:

- LiteLLM URL and API key (from a Kubernetes Secret).
- Model name, service name, port.
- Optional metadata (embedding flag, max token length, notes).

Benefits:

- Minimal moving parts.
- Works with the current Kustomize-based workflow.
- Can be run manually or as a Job after deploys.

## Mid-Term (KServe + Registrar)

Use KServe for model serving and add a registrar controller/job that:

1. Watches `InferenceService` resources.
2. Detects Ready status.
3. Registers with LiteLLM automatically.

Flow:

1. Apply `InferenceService`.
2. KServe creates Deployment + Service + routing.
3. Registrar registers the service with LiteLLM.

Notes:

- KServe does not register models with LiteLLM on its own.
- Registrar can also handle delete events to clean up LiteLLM entries.

## Long-Term (Custom CRD Operator)

Create a `ModelDeployment` CRD with a controller that:

- Creates/updates vLLM Deployment + Service.
- Registers/de-registers with LiteLLM.
- Emits status conditions (`ServiceReady`, `Registered`).

CRD fields could include:

- `modelId`, `servedName`, `port`, `maxModelLen`
- `gpu`, `nodeSelector`, `serviceName`
- `litellm` config (url, secret name, project, tags)

Benefits:

- Single resource is the source of truth.
- Enables self-serve model onboarding.
- Supports GitOps and auditability.

## Suggested Next Step

Implement the short-term registrar script first, then evolve to KServe or a CRD
when the number of models and teams grows.

## GitOps with ArgoCD

Use ArgoCD to apply model and KServe resources from Git. This gives a clear
deployment pipeline, auditability, and supports policy checks before sync.

Key ideas:

- App per namespace or team (scoped to their models).
- Sync windows and manual approvals for high-risk changes.
- Use OPA/Kyverno policies to enforce resource limits and allowlists.
- Combine with the LLM-in-the-loop review for additional safety.

## LLM-in-the-Loop for GitOps

An LLM can assist reviews but should not be the sole gatekeeper. Use it to
summarize risk and surface unusual settings, while deterministic policies enforce
hard limits.

Recommended flow:

1. LLM reviews the PR and flags risks (oversized GPUs, untrusted model sources,
   egress usage, unusual settings).
2. Policy-as-code (OPA/Kyverno) enforces guardrails (max GPUs, allowed images,
   no hostPath, restricted egress).
3. Human approval for exceptions or high-risk changes.
4. Audit decisions and approvals.

Practical note:

- Use a stable, trusted reviewer model (via LiteLLM) to post PR comments with
  security risks and anomalies. Keep policy enforcement separate and mandatory.

How to post PR comments:

- GitHub Actions can run a review script on `pull_request` and use the GitHub
  API (or `gh`) to comment with the LLM's findings.
- A webhook service can do the same outside CI by listening to PR events and
  posting comments via the GitHub API.
