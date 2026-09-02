# CLI reference

The native plugin registers `hermes omp`; the wheel also exposes `hermes-omp` with the same operator parser. Public commands accept `--json` unless noted. Exit codes are 0 success, 1 operational/health failure, 2 usage, 3 not found, 4 conflict, and 5 validation.

- `doctor [--fix] [--dry-run] [--json]`: inspect executables, profile state, service backend selection, stale owner locks, and oversized logs. `--fix` safely rotates oversized inactive logs and refuses live writers.
- `create NAME --cwd DIR --model MODEL --mission TEXT [--project TEXT] [--platform ID] [--chat ID] [--topic ID] [--allowed-user ID] [--resume ID] [--restart-policy never|on-failure|always] [--omp-path PATH] [--omp-option OPTION] [--no-install] [--start] [--dry-run] [--json]`.
- `adopt NAME --inspection FILE --mission TEXT [routing options] [--restart-policy ...] [--omp-path PATH] [--no-install] [--start] [--dry-run] [--json]`: accept trusted inspection JSON containing `argv` and `cwd`; `argv` must contain explicit `--resume`. It neither inspects nor terminates the source process.
- `list [--json]`; `status NAME [--json]`: report stored state, heuristic ownership, queue depths, activity, and last error.
- `send NAME MESSAGE [--json]`: durably queue a follow-up prompt.
- `logs NAME [--lines N] [--since EPOCH] [--level LEVEL] [--follow] [--poll-interval SECONDS] [--json]`.
- `events NAME [--queue prompt,outbound,inbound] [--status LIST] [--limit N] [--json]`: inspect heuristically redacted queue data.
- `retry NAME ID --yes [--json]` or `retry NAME --all --yes [--json]`: requeue dead outbound items only; authorization is not bypassed.
- `export NAME FILE [--json]`; `import FILE [--conflict fail|rename|replace] [--dry-run] [--no-install] [--start] [--json]`: use the versioned JSON archive format. Archives require operator review and protection; redaction is not a confidentiality guarantee.
- `update NAME [--model VALUE] [--mission TEXT] [--platform ID] [--chat ID] [--topic ID] [--allowed-user ID] [--restart-policy ...] [--omp-option OPTION] [--apply-restart] [--dry-run] [--no-install] [--json]`: transactionally change session configuration. Active sessions require `--apply-restart`. This does not upgrade the distribution or native plugin.
- `stop NAME [--json]`; `restart NAME [--json]`; `remove NAME [--no-service] [--purge-logs] [--json]`. Removal retains logs unless purge is explicit.
- `config validate NAME [--json]`; `config template [--json]`; `completion bash|zsh|fish [--json]`.
- `inbound NAME --event-id ID --question-id ID --platform PLATFORM --chat CHAT --topic TOPIC --user USER --answer ANSWER [--json]`: queue, but do not yet accept, an inbound answer.
- `run NAME`: internal foreground supervisor entry point; no `--json`. Generated services instead invoke the runtime module with explicit `--root` and `--expected-session-id` arguments.

Names in this reference are session names. The Python distribution is `hermes-omp`; the native Hermes plugin id is `omp`.
