#!/usr/bin/env bash
set -euo pipefail

# verify-hardening.sh — fail-closed hardening gate for container runtimes
# Checks: non-root images, NoNewPrivileges, CapDrop=ALL, PrivateTmp/tmpfs,
#         bridge networking (no host), port >=1024 (single NET_BIND_SERVICE
#         exception), consistent host.docker.internal:host-gateway alias.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── Dependency: yq ────────────────────────────────────────────────
if ! command -v yq &>/dev/null; then
  echo "ERROR: yq is required but not found in PATH"
  exit 1
fi

# ── Runtime detection ─────────────────────────────────────────────
HAS_DOCKER=0; HAS_PODMAN=0; HAS_PODMAN_COMPOSE=0
command -v docker         &>/dev/null && HAS_DOCKER=1
command -v podman         &>/dev/null && HAS_PODMAN=1
command -v podman-compose &>/dev/null && HAS_PODMAN_COMPOSE=1

if (( HAS_DOCKER == 0 && (HAS_PODMAN == 0 || HAS_PODMAN_COMPOSE == 0) )); then
  echo "ERROR: no usable container runtime found (need docker or podman+podman-compose)"
  exit 1
fi

# ── Env-file resolution (.env → examples/.env.example → mktemp stub)
#    Precedence ensures ${VAR} interpolation is resolved before rendering.
ENV_FILE=""
_ENV_STUB_CREATED=0
if [[ -f .env ]]; then
  ENV_FILE=".env"
elif [[ -f examples/.env.example ]]; then
  ENV_FILE="examples/.env.example"
else
  ENV_FILE="$(mktemp "${TMPDIR:-/tmp}/verify-hardening-env.XXXXXX")"
  _ENV_STUB_CREATED=1
  : > "$ENV_FILE"
fi
trap '(( _ENV_STUB_CREATED )) && rm -f "$ENV_FILE"' EXIT

# ── Renderers (no stderr suppression — render failures are hard failures) ──
render_docker_compose() {
  if [[ -n "$ENV_FILE" ]]; then
    docker compose -f docker-compose.yml --env-file "$ENV_FILE" config
  else
    docker compose -f docker-compose.yml config
  fi
}

render_podman_compose() {
  # podman-compose may lack --env-file; export vars to the subprocess instead
  set -a
  # shellcheck disable=SC1090
  [[ -f "$ENV_FILE" ]] && source "$ENV_FILE"
  set +a
  podman-compose -f podman-compose.yml config
}

# ── Individual hardening checks ───────────────────────────────────

check_non_root_images() {
  local rendered="$1" tool="$2"
  echo "  [$tool] Checking non-root user specification…"
  local missing=""
  while IFS= read -r svc; do
    local has_user
    has_user="$(yq -r ".services.\"$svc\".user // \"\"" <<< "$rendered")"
    if [[ -z "$has_user" ]]; then
      missing="$missing $svc"
    fi
  done < <(yq -r '.services | keys[]' <<< "$rendered")
  if [[ -n "$missing" ]]; then
    echo "ERROR [$tool]: services missing 'user:' (non-root requirement):$missing"
    return 1
  fi
  echo "    ✓ All services specify a non-root user"
}

check_no_new_privileges() {
  local rendered="$1" tool="$2"
  echo "  [$tool] Checking NoNewPrivileges…"
  local missing=""
  while IFS= read -r svc; do
    local has_nnp
    has_nnp="$(yq -r "
      .services.\"$svc\".security_opt // [] |
      any(. == \"no-new-privileges:true\" or . == \"no-new-privileges\")
    " <<< "$rendered")"
    if [[ "$has_nnp" != "true" ]]; then
      missing="$missing $svc"
    fi
  done < <(yq -r '.services | keys[]' <<< "$rendered")
  if [[ -n "$missing" ]]; then
    echo "ERROR [$tool]: missing no-new-privileges for:$missing"
    return 1
  fi
  echo "    ✓ NoNewPrivileges set on all services"
}

check_cap_drop_all() {
  local rendered="$1" tool="$2"
  echo "  [$tool] Checking CapDrop=ALL…"
  local missing=""
  while IFS= read -r svc; do
    local has_drop_all
    has_drop_all="$(yq -r ".services.\"$svc\".cap_drop // [] | any(. == \"ALL\")" <<< "$rendered")"
    if [[ "$has_drop_all" != "true" ]]; then
      missing="$missing $svc"
    fi
  done < <(yq -r '.services | keys[]' <<< "$rendered")
  if [[ -n "$missing" ]]; then
    echo "ERROR [$tool]: missing cap_drop: ALL for:$missing"
    return 1
  fi
  echo "    ✓ CapDrop=ALL on all services"
}

check_private_tmp() {
  local rendered="$1" tool="$2"
  echo "  [$tool] Checking PrivateTmp (tmpfs /tmp)…"
  local missing=""
  while IFS= read -r svc; do
    local has_tmpfs
    has_tmpfs="$(yq -r "
      .services.\"$svc\".tmpfs // [] |
      any(. == \"/tmp\" or startswith(\"/tmp:\"))
    " <<< "$rendered")"
    if [[ "$has_tmpfs" != "true" ]]; then
      missing="$missing $svc"
    fi
  done < <(yq -r '.services | keys[]' <<< "$rendered")
  if [[ -n "$missing" ]]; then
    echo "ERROR [$tool]: missing tmpfs:/tmp for:$missing"
    return 1
  fi
  echo "    ✓ PrivateTmp (tmpfs /tmp) on all services"
}

check_bridge_networking() {
  local rendered="$1" tool="$2"
  echo "  [$tool] Checking bridge networking (no host mode)…"
  local host_mode=""
  while IFS= read -r svc; do
    local net_mode
    net_mode="$(yq -r ".services.\"$svc\".network_mode // \"\"" <<< "$rendered")"
    if [[ "$net_mode" == "host" ]]; then
      host_mode="$host_mode $svc"
    fi
  done < <(yq -r '.services | keys[]' <<< "$rendered")
  if [[ -n "$host_mode" ]]; then
    echo "ERROR [$tool]: network_mode: host is forbidden for:$host_mode"
    return 1
  fi
  echo "    ✓ No host-mode networking"
}

check_host_alias() {
  local rendered="$1" tool="$2"
  echo "  [$tool] Checking standardized host.docker.internal:host-gateway alias…"
  while IFS= read -r svc; do
    local extra_hosts
    extra_hosts="$(yq -r ".services.\"$svc\".extra_hosts // []" <<< "$rendered")"
    [[ "$extra_hosts" == "null" || "$extra_hosts" == "[]" ]] && continue

    # Every entry must be exactly host.docker.internal:host-gateway
    local non_standard
    non_standard="$(yq -r "
      .services.\"$svc\".extra_hosts[] |
      select(. != \"host.docker.internal:host-gateway\")
    " <<< "$rendered")" || true
    if [[ -n "$non_standard" ]]; then
      echo "ERROR [$tool]: service '$svc' uses non-standard extra_hosts:" \
           "'$non_standard' (only host.docker.internal:host-gateway is allowed)"
      return 1
    fi
  done < <(yq -r '.services | keys[]' <<< "$rendered")
  echo "    ✓ host.docker.internal:host-gateway alias consistent"
}

check_port_audit() {
  local rendered="$1" tool="$2"
  echo "  [$tool] Auditing bound ports (≥1024 or single NET_BIND_SERVICE exception)…"
  local privileged_ports=""
  local nbs_count=0 nbs_svc=""

  while IFS= read -r svc; do
    while IFS= read -r port_def; do
      [[ -z "$port_def" ]] && continue
      # Extract host-side port: formats include "8080:80", "8080:80/tcp", "8080"
      local host_port
      host_port="$(echo "$port_def" | grep -oP '^\d+(?=:|\s|$)' || true)"
      [[ -z "$host_port" ]] && continue
      [[ "$host_port" -ge 1024 ]] && continue

      # Privileged port — is NET_BIND_SERVICE granted?
      local has_nbs
      has_nbs="$(yq -r "
        .services.\"$svc\".cap_add // [] |
        any(. == \"NET_BIND_SERVICE\")
      " <<< "$rendered")"
      if [[ "$has_nbs" == "true" ]]; then
        nbs_count=$((nbs_count + 1))
        nbs_svc="$svc"
      else
        privileged_ports="$privileged_ports $svc:$host_port"
      fi
    done < <(yq -r ".services.\"$svc\".ports // [] | .[]" <<< "$rendered")
  done < <(yq -r '.services | keys[]' <<< "$rendered")

  if [[ -n "$privileged_ports" ]]; then
    echo "ERROR [$tool]: privileged ports without NET_BIND_SERVICE:$privileged_ports"
    return 1
  fi
  if (( nbs_count > 1 )); then
    echo "ERROR [$tool]: more than one service uses NET_BIND_SERVICE (max 1 allowed)"
    return 1
  fi
  echo "    ✓ Port audit passed"
}

# ── Quadlet checks ────────────────────────────────────────────────
check_quadlet() {
  local tool="quadlet"
  shopt -s nullglob
  local units=(*.container)
  shopt -u nullglob
  if (( ${#units[@]} == 0 )); then
    echo "=== Quadlet hardening ==="
    echo "  (no .container files found — skipping)"
    echo ""
    return 0
  fi

  echo "=== Quadlet hardening ==="
  local errors=0

  for unit in "${units[@]}"; do
    local base="${unit%.container}"

    # Non-root: RunAsUser= or User=
    if ! grep -qE '^(RunAsUser|User)=' "$unit"; then
      echo "ERROR [$tool] $unit: missing RunAsUser= or User= (non-root requirement)"
      errors=$((errors + 1))
    fi

    # NoNewPrivileges
    if ! grep -q '^NoNewPrivileges=true' "$unit"; then
      echo "ERROR [$tool] $unit: missing NoNewPrivileges=true"
      errors=$((errors + 1))
    fi

    # DropCapability=ALL
    if ! grep -q '^DropCapability=ALL' "$unit"; then
      echo "ERROR [$tool] $unit: missing DropCapability=ALL"
      errors=$((errors + 1))
    fi

    # PrivateTmp or Tmpfs=/tmp
    if ! grep -q '^PrivateTmp=true' "$unit" && ! grep -q '^Tmpfs=/tmp' "$unit"; then
      echo "ERROR [$tool] $unit: missing PrivateTmp=true or Tmpfs=/tmp"
      errors=$((errors + 1))
    fi

    # Network=host is forbidden
    if grep -q '^Network=host' "$unit"; then
      echo "ERROR [$tool] $unit: Network=host is forbidden"
      errors=$((errors + 1))
    fi

    # Standardized AddHost
    if grep -q '^AddHost=' "$unit"; then
      local add_host
      add_host="$(grep '^AddHost=' "$unit")"
      if [[ "$add_host" != "AddHost=host.docker.internal:host-gateway" ]]; then
        echo "ERROR [$tool] $unit: non-standard $add_host" \
             " (use AddHost=host.docker.internal:host-gateway)"
        errors=$((errors + 1))
      fi
    fi
  done

  if (( errors > 0 )); then
    echo "ERROR [$tool]: $errors violation(s) found"
    return 1
  fi
  echo "  ✓ All quadlet units pass hardening checks"
  echo ""
}

# ── Compose runner (shared logic for docker & podman-compose) ─────
run_compose_checks() {
  local tool="$1" renderer="$2" compose_file="$3"

  if [[ ! -f "$compose_file" ]]; then
    echo "=== $tool hardening ==="
    echo "  ($compose_file not found — skipping)"
    echo ""
    return 0
  fi

  echo "=== $tool hardening ==="
  echo "  Rendering config (no stderr suppression)…"
  local rendered
  rendered="$("$renderer")" || {
    echo "FATAL: $tool config render failed — this is a fail-closed gate"
    return 1
  }

  check_non_root_images   "$rendered" "$tool" || return 1
  check_no_new_privileges "$rendered" "$tool" || return 1
  check_cap_drop_all      "$rendered" "$tool" || return 1
  check_private_tmp       "$rendered" "$tool" || return 1
  check_bridge_networking "$rendered" "$tool" || return 1
  check_host_alias        "$rendered" "$tool" || return 1
  check_port_audit        "$rendered" "$tool" || return 1
  echo ""
}

# ── Main ──────────────────────────────────────────────────────────
FAIL=0

check_quadlet || FAIL=1

if (( HAS_DOCKER == 1 )); then
  run_compose_checks "docker-compose" render_docker_compose "docker-compose.yml" || FAIL=1
else
  echo "=== docker-compose hardening ==="
  echo "  (docker not found — skipping)"
  echo ""
fi

if (( HAS_PODMAN == 1 && HAS_PODMAN_COMPOSE == 1 )); then
  run_compose_checks "podman-compose" render_podman_compose "podman-compose.yml" || FAIL=1
else
  echo "=== podman-compose hardening ==="
  echo "  (podman or podman-compose not found — skipping)"
  echo ""
fi

# ── Verdict ───────────────────────────────────────────────────────
if (( FAIL == 1 )); then
  echo "FAILED: hardening checks did not pass — see errors above"
  exit 1
fi

echo "PASSED: all hardening checks passed"
exit 0
