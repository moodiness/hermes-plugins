# Changelog

## 0.3.0rc1 - 2026-09-02

- Added reviewed legacy-state migration with dry-run-by-default apply/adopt gates, change-only NDJSON session watching, private redacted offline diagnosis, and configuration-only session cloning.
- Added persisted `interactive`, `balanced`, `night`, and `strict` approval profiles; only safe recommended reversible choices can be answered automatically.
- Added optional HMAC-SHA256 archive authentication through key-file or environment-variable references and pre-mutation import verification.
- Added per-kind question/error/milestone/completion/restart notification controls with durable deduplication.
- Added duration and restart-window/cooldown budgets. Token/cost caps fail closed with truthful unavailable status when trustworthy public OMP RPC usage is absent.
- Added bounded redacted local transition NDJSON with no telemetry and an opt-in public-SDK Desktop dashboard for sessions, health, questions, and log summaries; actions require confirmation and validated CLI contracts.

- Replaced unavailable macOS `os.waitid` supervision with portable `Popen.poll`/`wait` cleanup while preserving process-group termination and owner-lock safety.
- Added private bounded structured NDJSON logs with cross-process rotation, retention, record truncation/redaction, delta filtering, rotation-aware reads, doctor remediation, and explicit log purge.
- Bounded launchd/systemd/Task Scheduler diagnostics and pinned the canonical reproducible release build.

- Delayed inbound and automatic-answer state commits until the OMP RPC line is flushed; added incremental UTF-8 JSONL framing and unparsed EOF handling.
- Added bounded owned-child/process-group cleanup, durable orphan ownership markers, non-destructive Windows liveness probes, and exact session-id service handoff.
- Serialized queue and session mutations across cooperating processes, refreshed queue reads from disk, and made create/import/update rollback preserve prior state and service definitions.
- Corrected launchd, systemd-user, and Windows Task Scheduler lifecycle commands, path quoting, XML encoding, explicit profile roots, and manager-aware restoration.
- Excluded virtual environments, caches, build output, distributions, and historical evidence from source archives; removed ignored manifest CLI metadata.
- Corrected installation to the Hermes 0.21.0 wheel-plus-manual-copy flow, with native plugin id `omp`, distribution name `hermes-omp`, and separate directory/runtime doctor commands.
- Documented explicit profile-root and expected-session identity handoff, inherited environment overrides, process cleanup scope, heuristic redaction/liveness checks, and archive confidentiality limits.
- Narrowed compatibility and validation language to the exact Hermes baseline, fake-process E2E, macOS local scope, and generator-only Linux/Windows coverage.

## 0.2.0rc1 - 2026-09-01

- Added JSON output and categorized exit codes for operator commands.
- Added queue/event inspection with heuristic redaction, explicit outbound dead-letter retry, and health/queue/error reporting.
- Added versioned export/import with conflict policies, schema checks, dry-run, and rollback; exported content still requires confidential handling.
- Added transactional session configuration updates with explicit live restart, plus create/adopt previews.
- Added bounded log following, safe doctor repair previews, config validation/templates, and shell completion generation.

## 0.1.0rc1 - 2026-09-01

- Added the standalone native plugin registration, schema-v2 session state, OMP RPC runtime, correlation/authorization, and durable outbox.
- Added a launchd backend and generated systemd-user/Windows definitions; no native Linux/Windows manager validation was performed.
- Added isolated subprocess E2E using fake OMP and fake Hermes executables, without real services, gateway traffic, restart, or reboot coverage.
- Added editable `.[dev]` installation with a pip 21.3+ bootstrap for Python 3.9 development environments.
