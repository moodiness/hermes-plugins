# Threat model and security audit

Runtime and service diagnostics use private NDJSON logs. Defaults are 10 MiB per file, five backups, fourteen-day retention, and a 256 KiB maximum record. Writes redact before serialization, truncate at the boundary, exclude stream/tool deltas, rotate under a cross-process lock, and create files with mode 0600. Finite positive internal overrides are available through `HERMES_OMP_LOG_MAX_BYTES`, `HERMES_OMP_LOG_BACKUPS`, `HERMES_OMP_LOG_RETENTION_DAYS`, and `HERMES_OMP_LOG_MAX_RECORD_BYTES`.

Trust boundaries include OMP output, project files, inbound messages, inherited environment variables, archive files, executable paths, and service-manager state. The implementation uses argv lists without shell interpolation, strict slugs, private atomic state files, route/sender/question correlation, replay IDs, question expiry, and FIFO queues. It does not read Hermes `state.db`, import gateway internals, call channel APIs directly, or place outbound message bodies in process arguments.

These controls have limits:

- The OMP child and outbound Hermes process inherit the supervisor's complete environment. `HERMES_OMP_BINARY`, `HERMES_OMP_HERMES`, and `HERMES_OMP_AUTO_ANSWER_SAFE=1` are production-effective overrides. They are not test-only hooks or security boundaries.
- Redaction uses key names and regular expressions. It can miss novel secret formats and does not make logs, events, or archives non-sensitive.
- Owner checks test recorded PID/PGID liveness. PID reuse, permissions, stale metadata, and unrelated occupants can produce false confidence; liveness is not executable identity.
- POSIX cleanup targets the exact new process group created for the supervised OMP child. Windows cleanup targets the direct child. Neither statement proves ownership of every descendant on every platform.
- Archives omit live PID fields and owner locks and redact recognized values, but retain session configuration and selected runtime state. Paths, prompts, routes, options, executable locations, or unrecognized secrets may remain. Optional HMAC-SHA256 authenticates canonical archive bytes and is verified before import mutation; it does not encrypt content. Keys are accepted only through file or environment-variable references, never literal command arguments.
- The file inbox relies on profile filesystem ownership. Queuing means only that an envelope was written; runtime authorization and correlation happen afterward.

Automatic safe-answer classification is heuristic and denies recognized risky actions; it is not a general approval system. Stored policy profiles can enable automatic handling only after that safe classifier accepts a recommended reversible option. Push/publish/review/comment/merge/deploy, permission or secret changes, payments, and destructive or privileged commands require explicit authorized handling. Hermes 0.21.0 exposes no documented public standalone approval API used by this plugin.

Keep the profile, diagnostic reports, and archives access-controlled; review executable overrides and service definitions; do not treat HMAC authentication as encryption, apparent redaction as confidentiality, or a live PID as an identity guarantee. Environment allowlisting and a broader Hermes approval integration remain deferred.
