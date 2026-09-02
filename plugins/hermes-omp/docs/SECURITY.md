# Threat model and security audit

Trust boundaries include OMP output, project files, inbound messages, inherited environment variables, archive files, executable paths, and service-manager state. The implementation uses argv lists without shell interpolation, strict slugs, private atomic state files, route/sender/question correlation, replay IDs, question expiry, and FIFO queues. It does not read Hermes `state.db`, import gateway internals, call channel APIs directly, or place outbound message bodies in process arguments.

These controls have limits:

- The OMP child and outbound Hermes process inherit the supervisor's complete environment. `HERMES_OMP_BINARY`, `HERMES_OMP_HERMES`, and `HERMES_OMP_AUTO_ANSWER_SAFE=1` are production-effective overrides. They are not test-only hooks or security boundaries.
- Redaction uses key names and regular expressions. It can miss novel secret formats and does not make logs, events, or archives non-sensitive.
- Owner checks test recorded PID/PGID liveness. PID reuse, permissions, stale metadata, and unrelated occupants can produce false confidence; liveness is not executable identity.
- POSIX cleanup targets the exact new process group created for the supervised OMP child. Windows cleanup targets the direct child. Neither statement proves ownership of every descendant on every platform.
- Archives omit live PID fields and owner locks and redact recognized values, but retain session configuration and selected runtime state. Paths, prompts, routes, options, executable locations, or unrecognized secrets may remain.
- The file inbox relies on profile filesystem ownership. Queuing means only that an envelope was written; runtime authorization and correlation happen afterward.

Automatic safe-answer classification is heuristic and denies recognized risky actions; it is not a general approval system. Push/publish/review/comment/merge/deploy, permission or secret changes, payments, and destructive or privileged commands require explicit authorized handling. Hermes 0.21.0 exposes no documented public standalone approval API used by this plugin.

Keep the profile and archives access-controlled, review executable overrides and service definitions, and do not treat an apparently redacted output or a live PID as a security guarantee. Environment allowlisting, archive-content policy changes, and a broader approval integration remain deferred.
