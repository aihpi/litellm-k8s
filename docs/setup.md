# Setup

## Prerequisites

- Kubernetes cluster access
- kubectl and kustomize installed
- Container registry access if you publish custom images

## Install

```bash
kubectl apply -f namespaces/litellm.yaml
kubectl apply -k base/
kubectl apply -k models/
kubectl apply -k overlays/dev/
```

## Verify

```bash
kubectl get pods -n litellm
kubectl get svc -n litellm
```
