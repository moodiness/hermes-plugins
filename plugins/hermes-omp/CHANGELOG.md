# Changelog

## 0.2.0rc1 - 2026-09-01

- Added consistent `--json` output and stable exit-code categories across user commands.
- Added redacted queue/event inspection, explicit idempotent outbound dead-letter retry, and health/queue/error observability.
- Added versioned secret-free export/import with conflict policies, schema checks, dry-run, and rollback.
- Added transactional mutable updates with explicit live restart, plus create/adopt dry-run service previews.
- Added bounded log following and filters, safe doctor repairs/dry-run, config validation/templates, and standalone shell completion generation.

## 0.1.0rc1 - 2026-09-01

- Standalone public Hermes plugin and complete OMP CLI.
- Durable schema-v2 sessions, OMP RPC runtime, correlation/authorization, redaction and outbox.
- launchd backend; tested systemd/Windows definitions.
- Isolated deterministic E2E suite and publishing documentation.
- Fixed RC review blockers: durable follow-up RPC drain, restart-safe pending questions and replay protection, stale owner recovery, truthful inbound acknowledgement/rejection, Windows restart-policy generation, transactional create/adopt rollback, and stop-proven removal.
- Added editable `.[dev]` installation so plain venv `pytest` works without `PYTHONPATH`, including an explicit pip 21.3+ bootstrap for Python 3.9 venvs seeded with pip 21.2.4.
