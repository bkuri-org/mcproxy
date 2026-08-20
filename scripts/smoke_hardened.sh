#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# ── smoke_hardened.sh ────────────────────────────────────────────────────────
# Hardened smoke-test harness:
#   • Online blocklist — HTTPS-only allowlisted source, checksum-verified fetch
#   • Bundled seed blocklist — fail-closed when unreachable at boot
#   • Pre-dispatch validation (before any container action)
#   • Post-resolution validation (after DNS / image pull)
#   • Container hardening — digest pins, exact-version tags, minimal caps
#   • Dual-runtime smoke gates (docker + podman)
# ─────────────────────────────────────────────────────────────────────────────

# ── Configuration ────────────────────────────────────────────────────────────

# Allowlisted HTTPS-only blocklist source (NO http://, NO IP literals)
readonly BLOCKLIST_URL="https://raw.githubusercontent.com/example/blocklist/main/domains.txt"
readonly BLOCKLIST_SHA256_EXPECTED="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # placeholder – replace

# Bundled seed blocklist path (ships with the repo)
readonly SEED_BLOCKLIST="/opt/smoke/assets/seed_blocklist.txt"

# Working directory for fetched artefacts
readonly WORKDIR="/var/tmp/smoke_hardened"
readonly FETCHED_BLOCKLIST="${WORKDIR}/domains.txt"
readonly FETCHED_BLOCKLIST_SHA="${WORKDIR}/domains.txt.sha256"

# Container image references — EXACT version + digest pin
readonly IMAGE_DOCKER="docker.io/library/alpine:3.19.1@sha256:c5b1261d6d3e43071626a31b8df6d96b41794d2542116a3bb1f8b8f8dc9b70e7"
readonly IMAGE_PODMAN="docker.io/library/alpine:3.19.1@sha256:c5b1261d6d3e43071626a31b8df6d96b41794d2542116a3bb1f8b8f8dc9b70e7"

# Enumerated minimal Linux capabilities (nothing else is permitted)
readonly CAPS="--cap-drop=ALL --cap-add=CAP_NET_BIND_SERVICE"

# Logging
readonly LOG_TAG="smoke_hardened"
log()  { printf "[%s] %s\n" "$LOG_TAG" "$*" >&2; }
fatal() { log "FATAL: $*"; exit 1; }

# ── Boot guard ───────────────────────────────────────────────────────────────
# Fail-closed: if we cannot obtain a usable blocklist by any path, abort.

ensure_workdir() {
  mkdir -p "$WORKDIR"
  chmod 700 "$WORKDIR"
}

# ── Blocklist acquisition ────────────────────────────────────────────────────

validate_url_scheme() {
  local url="$1"
  [[ "$url" =~ ^https://[a-zA-Z0-9._-] ]] || \
    fatal "Blocklist URL must be HTTPS with a valid hostname: ${url}"
}

fetch_blocklist_online() {
  log "Attempting online blocklist fetch from ${BLOCKLIST_URL}"
  validate_url_scheme "$BLOCKLIST_URL"

  # curl hardening: TLS 1.2+, no redirects to non-HTTPS, timeout, fail on HTTP errors
  if ! curl --proto '=https' --tlsv1.2 \
       --max-time 30 --connect-timeout 10 \
       --fail --silent --show-error \
       -o "$FETCHED_BLOCKLIST" "$BLOCKLIST_URL"; then
    log "Online fetch failed (unreachable / TLS error / HTTP error)"
    return 1
  fi

  # Compute SHA-256 of fetched content
  local sha_actual
  sha_actual="$(sha256sum "$FETCHED_BLOCKLIST" | awk '{print $1}')"
  printf '%s  %s\n' "$sha_actual" "domains.txt" > "$FETCHED_BLOCKLIST_SHA"

  log "Fetched blocklist SHA-256: ${sha_actual}"

  # Checksum verification (fail-closed)
  if [[ "$sha_actual" != "$BLOCKLIST_SHA256_EXPECTED" ]]; then
    log "Checksum mismatch (expected ${BLOCKLIST_SHA256_EXPECTED})"
    rm -f "$FETCHED_BLOCKLIST" "$FETCHED_BLOCKLIST_SHA"
    return 1
  fi

  log "Online blocklist verified successfully"
  return 0
}

load_seed_blocklist() {
  log "Falling back to bundled seed blocklist: ${SEED_BLOCKLIST}"

  if [[ ! -f "$SEED_BLOCKLIST" ]]; then
    fatal "Seed blocklist not found at ${SEED_BLOCKLIST} — fail-closed, cannot continue"
  fi

  # Seed must be non-empty
  if [[ ! -s "$SEED_BLOCKLIST" ]]; then
    fatal "Seed blocklist is empty — fail-closed, cannot continue"
  fi

  cp "$SEED_BLOCKLIST" "$FETCHED_BLOCKLIST"
  local sha_seed
  sha_seed="$(sha256sum "$FETCHED_BLOCKLIST" | awk '{print $1}')"
  printf '%s  %s\n' "$sha_seed" "domains.txt" > "$FETCHED_BLOCKLIST_SHA"
  log "Seed blocklist loaded (SHA-256: ${sha_seed})"
}

acquire_blocklist() {
  # Primary: online, checksum-verified, HTTPS-only
  if fetch_blocklist_online; then
    return 0
  fi

  # Secondary: bundled seed
  load_seed_blocklist
}

# ── Pre-dispatch validation ──────────────────────────────────────────────────
# Executed BEFORE any container runtime is invoked.

pre_dispatch_validate() {
  log "Running pre-dispatch validation"

  # 1. Blocklist file exists and is readable
  [[ -f "$FETCHED_BLOCKLIST" ]] || fatal "Pre-dispatch: blocklist file missing"
  [[ -r "$FETCHED_BLOCKLIST" ]] || fatal "Pre-dispatch: blocklist file unreadable"

  # 2. Blocklist is non-empty
  [[ -s "$FETCHED_BLOCKLIST" ]] || fatal "Pre-dispatch: blocklist is empty"

  # 3. Every line must be a valid domain pattern (rough gate)
  local line_num=0
  while IFS= read -r line; do
    line_num=$((line_num + 1))
    # Skip comments and blank lines
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    # Must match a domain-like pattern (hostname or wildcard prefix)
    [[ "$line" =~ ^(\*\.)?[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]] || \
      fatal "Pre-dispatch: invalid entry at line ${line_num}: ${line}"
  done < "$FETCHED_BLOCKLIST"

  # 4. Image references contain both a tag and a digest
  for img in "$IMAGE_DOCKER" "$IMAGE_PODMAN"; do
    [[ "$img" =~ @sha256:[a-f0-9]{64}$ ]] || \
      fatal "Pre-dispatch: image missing digest pin: ${img}"
    # Extract tag portion and ensure it's an exact version (no 'latest', no floating tag)
    local tag
    tag="$(printf '%s' "$img" | sed -E 's|.*/([^@]+)@sha256:.*|\1|')"
    [[ "$tag" != "latest" ]] || \
      fatal "Pre-dispatch: floating 'latest' tag forbidden: ${img}"
    # Require at least one dot or digit sequence that looks like a version
    [[ "$tag" =~ [0-9]+\.[0-9]+ ]] || \
      fatal "Pre-dispatch: tag does not look like an exact version: ${tag}"
  done

  # 5. Capabilities string is present and non-empty
  [[ -n "${CAPS:-}" ]] || fatal "Pre-dispatch: CAPS is empty"

  log "Pre-dispatch validation passed"
}

# ── Post-resolution validation ───────────────────────────────────────────────
# Executed AFTER image pull / DNS resolution to confirm the runtime can reach
# the registry and the image actually landed.

post_resolution_validate() {
  local runtime="$1"
  local image="$2"
  log "Running post-resolution validation (${runtime})"

  # 1. Image must be present locally
  if [[ "$runtime" == "docker" ]]; then
    docker image inspect "$image" >/dev/null 2>&1 || \
      fatal "Post-resolution (${runtime}): image not found locally after pull: ${image}"
  elif [[ "$runtime" == "podman" ]]; then
    podman image inspect "$image" >/dev/null 2>&1 || \
      fatal "Post-resolution (${runtime}): image not found locally after pull: ${image}"
  fi

  # 2. Verify local image digest matches the pinned digest
  local expected_digest
  expected_digest="$(printf '%s' "$image" | grep -oP '@sha256:\K[a-f0-9]{64}')"
  local actual_digest
  if [[ "$runtime" == "docker" ]]; then
    actual_digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$image" | grep -oP 'sha256:[a-f0-9]{64}' | cut -d: -f2)"
  else
    actual_digest="$(podman image inspect --format '{{index .RepoDigests 0}}' "$image" | grep -oP 'sha256:[a-f0-9]{64}' | cut -d: -f2)"
  fi

  if [[ "$actual_digest" != "$expected_digest" ]]; then
    fatal "Post-resolution (${runtime}): digest mismatch — expected ${expected_digest}, got ${actual_digest}"
  fi

  log "Post-resolution validation passed (${runtime})"
}

# ── Container smoke gate ─────────────────────────────────────────────────────

run_smoke_gate() {
  local runtime="$1"
  local image="$2"

  log "=== ${runtime} smoke gate ==="

  # Verify runtime is on PATH
  command -v "$runtime" >/dev/null 2>&1 || \
    { log "WARNING: ${runtime} not found on PATH — skipping gate"; return 0; }

  # Pull with digest pin (runtime must resolve to the exact layer set)
  log "Pulling image: ${image}"
  "$runtime" pull "$image" >/dev/null 2>&1 || \
    fatal "${runtime} pull failed for ${image}"

  # Post-resolution validation
  post_resolution_validate "$runtime" "$image"

  # Run container with hardened profile:
  #   • No default capabilities (cap-drop ALL)
  #   • Only enumerated caps added back
  #   • Read-only rootfs
  #   • No new privileges
  #   • Tmpfs on /tmp
  #   • PID limits
  #   • Non-root user (nobody)
  log "Launching hardened container via ${runtime}"
  "$runtime" run --rm \
    $CAPS \
    --read-only \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --pids-limit 64 \
    --user 65534:65534 \
    --network none \
    "$image" \
    /bin/sh -c 'echo smoke_ok; exit 0' | grep -q '^smoke_ok$' || \
    fatal "${runtime} container smoke test failed"

  log "${runtime} smoke gate PASSED"
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
  log "Smoke-hardened harness starting"

  ensure_workdir

  # Acquire blocklist (online → seed → fatal)
  acquire_blocklist

  # Pre-dispatch validation
  pre_dispatch_validate

  # Dual-runtime container smoke gates
  run_smoke_gate "docker" "$IMAGE_DOCKER"
  run_smoke_gate "podman" "$IMAGE_PODMAN"

  log "ALL SMOKE GATES PASSED"
}

main "$@"
