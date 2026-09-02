# Acceptance requirements

Source: user request and recovered full specification from local Hermes session.

- Standalone third-party Hermes plugin using plugin.yaml and ctx.register_cli_command.
- Commands: doctor/create/adopt/list/status/send/logs/stop/restart/remove.
- Independent durable runtime; no state.db; no Telegram token/API.
- Outbound delivery only through `hermes send`, message on stdin.
- Replaceable inbound public interface; upstream generic hook proposal if absent.
- OMP RPC, strict question correlation, authorization, approvals, durable outbox, redaction.
- Functional launchd; generated/tested systemd-user and Windows user-task backends.
- Temporary HERMES_HOME, deterministic fake OMP and fake Hermes bridge E2E; no real restart.
- Skill, docs, packaging, CI, local release candidate.
- Per-kind notification controls with durable deduplication.
- Duration and restart-window/cooldown budgets; token/cost caps only with trustworthy public RPC usage, otherwise truthful fail-closed status.
- Bounded redacted local transition NDJSON with no telemetry.
- Documented opt-in public-SDK Desktop dashboard, read-only by default, with confirmed safe backend/CLI action contracts.
- No push or remote release; no active process modification.
