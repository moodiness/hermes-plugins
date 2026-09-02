---
name: omp-service
description: Supervise durable OMP sessions through Hermes safely.
version: 0.3.0rc1
author: hermes-omp contributors, Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [OMP, Supervision, RPC]
    related_skills: []
---

# OMP Service Skill

Supervise long-running OMP RPC sessions without coupling their lifetime to Hermes or its gateway. This skill never reads Hermes internal state and never handles channel credentials.

## When to Use

- Use for an OMP mission that must survive Hermes or gateway restarts.
- Use to resume an explicitly identified OMP session.
- Don't use for a short foreground OMP command or to migrate a live owner.

## Prerequisites

- The `hermes_omp` plugin and package are installed.
- `hermes` and `omp` are available.
- The destination and allowed sender IDs are known without reading secrets.

## How to Run

Invoke `terminal(command="hermes omp doctor --json")`. Proceed only when `ok` is true.

## Quick Reference

- `terminal(command="hermes omp list --json")`
- `terminal(command="hermes omp status NAME --json")`
- `terminal(command="hermes omp send NAME 'instruction'")`
- `terminal(command="hermes omp logs NAME --lines 100")`
- `terminal(command="hermes omp events NAME --status dead,rejected --json")`
- `terminal(command="hermes omp export NAME archive.json --json")`
- `terminal(command="hermes omp update NAME --model MODEL --dry-run --json")`
- `terminal(command="hermes omp watch NAME --json")`
- `terminal(command="hermes omp diagnose NAME --output diagnosis.json --json")`
- `terminal(command="hermes omp clone NAME COPY --no-install --json")`
- `terminal(command="hermes omp migrate-legacy NAME --source REVIEWED.json --json")`
- `terminal(command="hermes omp stop NAME")`

## Procedure

1. Run doctor and list; confirm dependencies and no duplicate owner.
2. Create with explicit cwd, model, mission, route, allowed user, and restart policy; confirm state is `created`.
   Select notification kinds and local duration/restart budgets explicitly. Treat nonzero token/cost caps as unavailable and fail closed until status identifies trustworthy public RPC usage.
3. Start through the installed user service; confirm one supervisor and one OMP PID.
4. For an OMP question, preserve its correlation ID and route the answer through the configured public inbound adapter.
5. Stop gracefully and verify the named process is inactive before removal.
6. Inspect dead letters before running `retry NAME ID --yes`; never retry inbound authorization failures.
7. Preview create/adopt/import/update/doctor/migrate-legacy/clone changes before service or state changes. Legacy migration requires `--apply`; retaining its resume identity separately requires `--adopt`.

## Pitfalls

- Logs are private bounded NDJSON. `remove` retains them; use `--purge-logs` only after confirming the session is inactive and retained evidence is no longer needed. `doctor --fix` refuses a live writer.
- Never delete ownership locks to bypass a live owner.
- Never auto-answer publication, review, merge, deployment, secrets, permissions, payment, privileged, or destructive actions.
- Session archives remain sensitive. HMAC-SHA256 can detect modification but does not encrypt; pass key material only by `--hmac-key-file` or `--hmac-key-env` reference.
- `balanced` and `night` policies can automatically answer only recommended reversible choices classified safe. Sensitive actions remain explicit under every profile.
- RC1 validates the runtime on macOS only; Linux and Windows backends are definition-tested.
- Transition logs are bounded redacted local NDJSON; no telemetry is sent. The Desktop dashboard is opt-in and read-only by default; confirmed actions must use its validated CLI contract.

## Verification

- Doctor reports `state_db_used: false` and `telegram_api_used: false`.
- Status records exact cwd, route, versions and OMP session ID.
- A bridge outage queues events and recovery drains them once in FIFO order.
- No active legacy session is modified during adoption.
