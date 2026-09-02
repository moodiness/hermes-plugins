# Incident recovery

1. Request a stop with `hermes omp stop NAME`, then inspect `hermes omp status NAME --json` until `active` is false. Do not signal processes selected only by name or a guessed PID.
2. Copy `$HERMES_HOME/omp/{sessions,run,outbox,logs,inbox,quarantine}` to private offline storage. Treat the copy as sensitive even when displayed data appears redacted.
3. Run `hermes omp doctor --json` and inspect the named session with `hermes omp events NAME --queue prompt,outbound,inbound --json` and `hermes omp logs NAME --lines 100`.
4. Confirm the stored OMP resume id, expected session record id, and route. PID/PGID liveness checks are reuse-prone heuristics and cannot prove executable identity.
5. Restore only a reviewed schema-v2 state set atomically, or preview a trusted adoption with `hermes omp adopt NAME --inspection FILE --mission TEXT --dry-run --json` before executing it.
6. Request restart with `hermes omp restart NAME`, then verify `hermes omp status NAME --json`, one expected owner, ordered outbox drain, and a correlated test reply.
7. After repairing the delivery failure, inspect dead outbound items before `hermes omp retry NAME ID --yes --json`. Retain rejected/dead records required for audit.

Generated services pin state with explicit runtime `--root` and hand off `--expected-session-id`, so a replaced record is rejected at startup. Owner locks contain a supervisor PID, session id, random token, and child PID/PGID marker. On POSIX cleanup signals the exact process group created for the OMP child; on Windows it terminates the direct child. These mechanisms reduce accidental cross-session cleanup but do not establish cryptographic process identity. Never manually unlink a lock while its recorded PID/PGID may still be live.

Export archives are portable session records, not complete confidential backups. They omit live PID fields and locks and apply heuristic redaction, yet can retain sensitive configuration, paths, prompts, executable locations, and unrecognized secrets.
