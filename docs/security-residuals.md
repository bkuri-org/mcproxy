# Security Residuals

## Accepted Residual: Same-UID /proc/environ and Inspect-Command Secret Exposure

### Vector

When the container process runs as UID 1000 (pinned literal), any process on the host
that can read `/proc/<container-pid>/environ` — or invoke `systemctl show` /
`podman inspect` against the unit or container — can observe environment variables
passed to the service. Because secrets are currently delivered exclusively via
`Environment=` lines in a root-owned 0600 drop-in (inside a 0700 root:root directory),
those secrets are present in the process environment and thus reachable through these
introspection channels.

### Classification

- **Bouncer ID:** #3 (REPEATED, low)
- **Severity:** Low — requires local host access as a user capable of reading
  `/proc/<pid>/environ` (typically root or same-UID) or invoking systemd/podman
  inspection commands.
- **Exploitability:** Limited to users who already have sufficient host-level
  privilege to inspect the container or its systemd unit.

### Decision

**No code remediation required.** The bouncer's finding confirms that the current
hardening posture — read-only root, tmpfs `/tmp`, `ro,Z` config / `rw,Z` data,
pinned literal UID/GID 1000:1000 matched by deploy-side `chown 0700`, secret-byte
exclusion from `/tmp` and the config volume, env-driven fail-closed DB path with
`MCPROXY_DATA_DIR=/data` always set, and exhaustive writable-path release gate —
already meets the project's threat model. The residual is formally accepted.

### Enforcement

`deploy.sh` re-verifies every hardening enforcement point on each run:

1. Root filesystem is read-only.
2. `/tmp` is a tmpfs mount.
3. Config volume is mounted `ro,Z`.
4. Data volume is mounted `rw,Z`.
5. Container runs as UID:GID `1000:1000` (pinned literal).
6. Data directory is owned `1000:1000` with mode `0700`.
7. Drop-in directory is owned `root:root` with mode `0700`.
8. Drop-in file is owned `root:root` with mode `0600`.
9. No secret bytes appear in `/tmp` or on the config volume.
10. `MCPROXY_DATA_DIR=/data` is set (fail-closed DB path).
11. All writable paths pass the release gate.

Additionally, `deploy.sh` bounds the exposure class by:

- **Warning if host UID 1000 is an interactive login:** If `getent passwd 1000`
  indicates a login shell, a warning is emitted noting that an interactive user
  sharing the container's UID could trivially read `/proc/<pid>/environ` without
  elevated privilege.
- **Emitting the residual verification block to the deploy journal:** Each run
  logs the full set of enforcement checks and the accepted-residual acknowledgment
  to the systemd journal (or equivalent) for auditability.

### Escalation / Future Mitigation

If the threat model changes or deployment moves to a multi-tenant host where
same-UID local access is not acceptable, the following upstream mechanisms are
available but are **out of scope** for the current release:

- **systemd `LoadCredential=`:** Passes secrets via file descriptors rather than
  the process environment, removing them from `/proc/<pid>/environ` and
  `systemctl show` output entirely.
- **podman `--secret`:** Uses the container runtime's secret store (mounted as
  files under `/run/secrets/`), similarly excluding secrets from environment
  introspection.

Adopting either mechanism would require refactoring the secret-delivery path
in the drop-in generator and is deferred unless the residual is reclassified.
