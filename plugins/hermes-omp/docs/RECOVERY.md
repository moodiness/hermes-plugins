# Incident recovery

1. Stop the named user service; do not signal unrelated OMP processes.
2. Copy `$HERMES_HOME/omp/{sessions,run,outbox,logs}` to private offline storage.
3. Run doctor and inspect redacted logs/quarantine.
4. Confirm the exact OMP session ID and that no other owner is live.
5. Restore a reviewed valid schema-v2 state atomically, or use `adopt` from trusted inspection.
6. Restart and verify status, one owner, ordered outbox drain, and a correlated test reply.
7. Retain dead letters for audit.
