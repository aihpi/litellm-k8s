# Sealed Secrets for namespace kiseno-tools

Two Secrets, created and sealed by an admin on the deploy host. Run the commands in the checkout (`~/k8-deployments/litellm-k8s`). The values are typed into a hidden prompt, so they do not end up in the shell history. Only the sealed files are committed.

After sealing, commit both files and remove the `#` in front of their entries in `../kustomization.yaml`.

## agent-toolbox-registry (image pull)

The GitLab project is private, so the cluster needs a deploy token of `kisz/arbeitsagentur/agent-toolbox` with the scope `read_registry` (GitLab: Settings, Repository, Deploy tokens).

```bash
read -r  -p "Deploy token username: " REG_USER
read -rs -p "Deploy token: " REG_TOKEN; echo
kubectl create secret docker-registry agent-toolbox-registry \
  --namespace kiseno-tools \
  --docker-server=registry.gitlab.hpi.de \
  --docker-username="$REG_USER" --docker-password="$REG_TOKEN" \
  --dry-run=client -o yaml \
  | kubeseal --format yaml --controller-namespace sealed-secrets \
  > overlays/prod/kiseno-tools/sealed-secrets/agent-toolbox-registry.yaml
unset REG_USER REG_TOKEN
```

## eventbrite-mcp-secret

`EVENTBRITE_TOKEN` is the private token of the Eventbrite account whose events the agents may read.

```bash
read -rs -p "Eventbrite private token: " EB_TOKEN; echo
kubectl create secret generic eventbrite-mcp-secret \
  --namespace kiseno-tools \
  --from-literal=EVENTBRITE_TOKEN="$EB_TOKEN" \
  --dry-run=client -o yaml \
  | kubeseal --format yaml --controller-namespace sealed-secrets \
  > overlays/prod/kiseno-tools/sealed-secrets/eventbrite-mcp.yaml
unset EB_TOKEN
```

The server also accepts an optional key `MCP_BEARER_TOKEN` in the same Secret. If it is set, the same value has to be entered as bearer token in the server's entry in LiteLLM. It is a second barrier next to the NetworkPolicy; add it with a further `--from-literal` if wanted.
