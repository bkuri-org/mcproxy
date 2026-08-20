# PROJECT.md — Canonical Status Home

> **Single source of truth.** This file replaces GOALS.md (deleted) and
> ROADMAP.md (trimmed to a historical note). All tooling, CI, and
> documentation MUST reference this file for project-level status.

---

## Owner & Deploy

| Field | Value | Source |
|-------|-------|--------|
| Owner | TBD | deploy.sh / docker-compose.yml (cross-check `.github/workflows/deploy.yml`) |
| Repository | TBD | local `git remote get-url origin` |
| Deploy target | TBD | deploy.sh |
| Image / service name | TBD | docker-compose.yml |
| Registry | TBD | docker-compose.yml / deploy.sh |

> Fields marked **TBD** are unconfirmed; populate after cross-checking
> `deploy.sh`, `docker-compose.yml`, and `.github/workflows/deploy.yml`
> in a single pass.

---

## Current Status

**Phase:** TBD

### Active

- (none recorded)

### Blocked

- (none recorded)

### Deferred

- (none recorded)

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| (today) | Deprecate GOALS.md and ROADMAP.md; consolidate into PROJECT.md | Eliminate split-source drift; single grep gate ensures no tooling regresses |

---

## Tooling Grep Gate

Both the GOALS.md deletion and the ROADMAP.md trim are gated on a **single
symmetric grep** that MUST return zero matches before the commit lands:

```
grep -rnE 'GOALS\.md|ROADMAP\.md' \
  .github/workflows/ \
  .beads/hooks/ \
  *.sh \
  Makefile \
  *.yml \
  *.yaml
```

- **Generators** (scripts/templates that emit references to GOALS.md or
  ROADMAP.md): remove or mute the relevant lines.
- **Unconditional readers** (CI steps, Make targets, hooks that `cat` or
  `include` either file): guard with a existence check or redirect to
  `PROJECT.md`.

---

## Migrated Content

### From GOALS.md

*(GOALS.md has been deleted. Any goals not yet captured as actionable
items belong in the issue tracker, not a separate goals file.)*

### From ROADMAP.md

*(ROADMAP.md is retained only as a superseded v4.x historical note — see
below. All forward-looking roadmap information now lives in this file
under "Current Status" and the issue tracker.)*

---

## Reference: Superseded Documents

### ROADMAP.md

> **Historical note — v4.x era only.**
>
> This file is superseded by [PROJECT.md](PROJECT.md). It is retained
> solely for historical context regarding the v4.x release line. Do not
> add new roadmap items here.

### GOALS.md

> **Deleted.** Content that was actionable has been triaged into issues.
> Aspirational content without clear acceptance criteria has been
> discarded per GTD principles.

---

## Dangling-Reference Audit

The following known reference patterns were corrected in the same commit
that introduced this file:

- `README.md` links to GOALS.md → redirected to `PROJECT.md`
- `README.md` links to ROADMAP.md → redirected to `PROJECT.md`
- `CONTRIBUTING.md` "see GOALS.md" → redirected to `PROJECT.md`
- `.github/ISSUE_TEMPLATE/*.md` references to ROADMAP.md → redirected to `PROJECT.md`

If additional references are discovered, fix them in-place pointing to
`PROJECT.md` and add an entry here.

---

*Last updated: (commit date)*
*Maintained by: (see Owner field above)*
