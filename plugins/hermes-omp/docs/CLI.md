# CLI reference

- Every user command accepts `--json`. Exit codes are stable: 0 success, 1 operational/health failure, 2 usage, 3 not found, 4 conflict, 5 validation.
- `doctor [--fix] [--dry-run] [--json]`: dependencies, state, service backend, prohibited-coupling indicators, and safe repairs only (private permissions, missing directories, dead-PID locks).
- `create NAME --cwd DIR --model MODEL --mission TEXT [routing/options]`: persist and optionally install/start.
- `adopt NAME --inspection FILE --mission TEXT`: adopt only inspected argv with explicit `--resume ID`; does not kill a source process.
- `list [--json]`, `status NAME [--json]`: health, queue depths, last activity/error.
- `logs NAME [--lines N] [--since EPOCH] [--level LEVEL] [--follow]`: filtered logs and clean interruptible polling.
- `events NAME [--queue prompt,outbound,inbound] [--status ...] [--limit N]`: redacted queue inspection.
- `retry NAME ID --yes` or `retry NAME --all --yes`: requeue dead outbound items only; authorization is never bypassed.
- `export NAME FILE`, `import FILE [--conflict fail|rename|replace] [--dry-run]`: versioned portable archives without secrets, PID state, or locks.
- `update NAME [mutable options] [--dry-run] [--apply-restart]`: transactional service regeneration; IDs remain immutable and active sessions require explicit stop/reinstall/restart.
- `config validate NAME`, `config template`, `completion bash|zsh|fish`.
- `send NAME MESSAGE`: durable follow-up queue; the runtime acknowledges an item only after its OMP RPC frame is flushed.
- `stop NAME`, `restart NAME`, `remove NAME`.
- `inbound NAME ...`: replaceable public inbound bridge entry point. Output says `queued`, not `accepted`; authorization, correlation, expiry, and answer validation happen in the runtime. Retryable events remain pending, terminal rejections move to `rejected/`, and delivered answers move to `processed/` only after the RPC response is flushed.
- `run NAME`: service-only supervisor entry point.

`adopt` intentionally requires an externally produced, trusted inspection JSON. Operators must stop the source only after proving the new service owns the same ID; RC1 never mutates unknown processes.
