#!/bin/bash
set -euo pipefail

# Creates the LiteLLM keys for the KISENO agent runner and the Qwen-Bot Slack
# bot and writes them as SealedSecrets. Run on the deploy host from the repo
# root:
#
#   scripts/seal-kiseno-secrets.sh runner   # overlays/prod/sealed-secrets/kiseno-agent-runner.yaml
#   scripts/seal-kiseno-secrets.sh bot      # overlays/prod/sealed-secrets/kiseno-slack-bot.yaml
#
# No key or token is printed, written to disk in clear text or left in the
# shell history. The LiteLLM keys are generated inside the litellm-proxy pod
# with its own LITELLM_MASTER_KEY; the Slack tokens are typed at a hidden
# prompt. The only thing that changes in the cluster is LiteLLM's database
# (one service user and one key per run). Nothing is applied: commit the
# written file and deploy as usual.
#
# Each service gets its own LiteLLM user with role proxy_admin, because a key
# takes the role of the user who owns it; a key without a user cannot manage
# agents or mint keys for other users. The key itself is limited by
# allowed_routes, has no team (so the kisz-key-expiry job never sees it) and
# no end date.
#
# To rotate: delete the old key in the LiteLLM UI (aliases kiseno-agent-runner
# and qwen-bot-slack), run this again, commit and deploy.

NAMESPACE=${NAMESPACE:-litellm}
SEALED_SECRETS_NAMESPACE=${SEALED_SECRETS_NAMESPACE:-sealed-secrets}
OUT_DIR=overlays/prod/sealed-secrets

if [ ! -d "$OUT_DIR" ]; then
  echo "Run this from the repo root ($OUT_DIR not found)." >&2
  exit 1
fi

# Prints the new key to stdout and nothing else. Runs inside the litellm
# container, which has LITELLM_MASTER_KEY in its environment.
mint_key() {
  kubectl -n "$NAMESPACE" exec -i deploy/litellm-proxy -c litellm -- python3 - "$@" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

user_id, key_alias, routes = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
base = "http://localhost:4000"
headers = {
    "Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"],
    "Content-Type": "application/json",
}


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fail(message):
    sys.stderr.write(message + "\n")
    sys.exit(1)


try:
    call("POST", "/user/new", {"user_id": user_id, "user_role": "proxy_admin", "auto_create_key": False})
except urllib.error.HTTPError as error:
    # 400/409: the user exists already. Its role is checked below either way.
    if error.code not in (400, 409):
        fail("/user/new for %s failed with HTTP %s" % (user_id, error.code))

try:
    info = call("GET", "/user/info?user_id=" + urllib.parse.quote(user_id))
except urllib.error.HTTPError as error:
    fail("/user/info for %s failed with HTTP %s" % (user_id, error.code))
role = (info.get("user_info") or {}).get("user_role") or info.get("user_role")
if role != "proxy_admin":
    fail("user %s has role %r, expected 'proxy_admin'; no key was created" % (user_id, role))

try:
    created = call("POST", "/key/generate", {"user_id": user_id, "key_alias": key_alias, "allowed_routes": routes})
except urllib.error.HTTPError as error:
    fail(
        "/key/generate for alias %s failed with HTTP %s. If a key with this alias exists, "
        "delete it in the LiteLLM UI first." % (key_alias, error.code)
    )
key = created.get("key")
if not key:
    fail("/key/generate returned no key")
if created.get("expires") is not None:
    fail(
        "the new key %s has an end date (%s); delete it in the LiteLLM UI and check "
        "default_key_generate_params / upperbound_key_generate_params" % (key_alias, created["expires"])
    )
sys.stdout.write(key)
PY
}

seal() {
  kubeseal --format yaml --controller-namespace "$SEALED_SECRETS_NAMESPACE"
}

read_hidden() {
  local prompt=$1 value
  read -rs -p "$prompt: " value
  echo >&2
  if [ -z "$value" ]; then
    echo "Empty value, stopping." >&2
    exit 1
  fi
  printf '%s' "$value"
}

case "${1:-}" in
  runner)
    out="$OUT_DIR/kiseno-agent-runner.yaml"
    sync_key=$(mint_key svc-kiseno-agent-runner kiseno-agent-runner \
      "agent_management_routes,/key/generate,/key/delete,/key/list")
    [ -n "$sync_key" ] || { echo "No key received, stopping." >&2; exit 1; }
    inbound_token=$(openssl rand -hex 32)
    kubectl create secret generic kiseno-agent-runner-secret --dry-run=client -o yaml -n "$NAMESPACE" \
      --from-literal=LITELLM_SYNC_KEY="$sync_key" \
      --from-literal=RUNNER_INBOUND_TOKEN="$inbound_token" \
      | seal > "$out.tmp"
    mv "$out.tmp" "$out"
    echo "Wrote $out"
    ;;
  bot)
    out="$OUT_DIR/kiseno-slack-bot.yaml"
    slack_app_token=$(read_hidden "Slack app-level token (xapp-...)")
    slack_bot_token=$(read_hidden "Slack bot token (xoxb-...)")
    case "$slack_app_token" in xapp-*) ;; *) echo "The app-level token must start with xapp-." >&2; exit 1 ;; esac
    case "$slack_bot_token" in xoxb-*) ;; *) echo "The bot token must start with xoxb-." >&2; exit 1 ;; esac
    mint_key_value=$(mint_key svc-qwen-bot-slack qwen-bot-slack \
      "/key/generate,/user/list,/team/info,/v1/agents")
    [ -n "$mint_key_value" ] || { echo "No key received, stopping." >&2; exit 1; }
    kubectl create secret generic kiseno-slack-bot-secret --dry-run=client -o yaml -n "$NAMESPACE" \
      --from-literal=SLACK_APP_TOKEN="$slack_app_token" \
      --from-literal=SLACK_BOT_TOKEN="$slack_bot_token" \
      --from-literal=LITELLM_MINT_KEY="$mint_key_value" \
      | seal > "$out.tmp"
    mv "$out.tmp" "$out"
    echo "Wrote $out"
    ;;
  *)
    echo "Usage: $0 runner|bot" >&2
    exit 1
    ;;
esac
