# Troubleshooting

Run `hermes omp doctor --json`, then `status` and `logs`. `hermes_send.ok=false` means Hermes is unavailable; events remain queued. A dead outbox item exhausted retries and requires fixing the bridge then controlled requeue. A corrupt state file is moved to `quarantine/`; recover from a reviewed backup rather than editing in place. `session already owned` prevents duplicate supervisors. Never bypass ownership by deleting the lock while its PID is live.
