# Incident recovery

1. Stop the named user service; do not signal unrelated OMP processes.
2. Copy `$HERMES_HOME/omp/{sessions,run,outbox,logs}` to private offline storage.
3. Run doctor and inspect redacted logs/quarantine.
4. Confirm the exact OMP session ID and that no other owner is live.
5. Restore a reviewed valid schema-v2 state atomically, or use `adopt` from trusted inspection.
6. Restart and verify status, one owner, ordered outbox drain, and a correlated test reply.
7. Retain dead letters for audit.

The owner lock stores the supervisor PID, session ID, and a random ownership token. A new supervisor recovers a stale lock only when its PID is no longer live and its session ID matches. Never unlink a live or foreign lock manually. Pending question details and replay IDs are persisted in `run/NAME.runtime.json` and survive supervisor restarts.
