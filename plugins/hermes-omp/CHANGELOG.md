# Changelog

## 0.1.0rc1 - 2026-09-01

- Standalone public Hermes plugin and complete OMP CLI.
- Durable schema-v2 sessions, OMP RPC runtime, correlation/authorization, redaction and outbox.
- launchd backend; tested systemd/Windows definitions.
- Isolated deterministic E2E suite and publishing documentation.
- Fixed RC review blockers: durable follow-up RPC drain, restart-safe pending questions and replay protection, stale owner recovery, truthful inbound acknowledgement/rejection, Windows restart-policy generation, transactional create/adopt rollback, and stop-proven removal.
- Added editable `.[dev]` installation so plain venv `pytest` works without `PYTHONPATH`.
