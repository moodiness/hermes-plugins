# Troubleshooting

Start with the two distinct checks: run `hermes plugins doctor "$HERMES_HOME/plugins/omp" --ci` for the copied native plugin directory, then `hermes omp doctor --json` for operational dependencies and state. The plugin id is `omp`; the installed Python distribution is `hermes-omp`; arguments to session commands are operator-chosen session names.

- `hermes_send.ok=false`: the selected Hermes executable is unavailable. Outbound events remain queued; inspect `HERMES_OMP_HERMES`, the service environment, and the explicit profile before retrying.
- Unexpected OMP executable: `HERMES_OMP_BINARY` overrides the stored `--omp-path`. Child processes inherit the supervisor's full environment.
- Wrong profile from a service: generated definitions pass explicit runtime `--root`; the outbound bridge derives child `HERMES_HOME` from it. Regenerate the session service from the intended profile rather than relying on an interactive shell's environment.
- `session identity changed`: the service's `--expected-session-id` no longer matches the record under its explicit root. Do not bypass the check; reconcile the profile and reviewed state.
- `session already owned` or orphan protection: inspect `hermes omp status NAME --json`. PID/PGID liveness is heuristic and reuse-prone; never delete a lock merely because the process name looks unfamiliar.
- Delivery failure: inspect `hermes omp events NAME --queue outbound --status dead --json`; repair the bridge, then explicitly run `hermes omp retry NAME ID --yes --json`.
- Inbound remains pending: `hermes omp inbound ...` reports queued, not authorized. Check route, sender, question id, expiry, and `hermes omp events NAME --queue inbound --json`.
- Corrupt state: preserve a private copy and recover from reviewed state or trusted adoption; do not edit live files in place.
- Signature validation failure: do not bypass it. Confirm the reviewed key reference and archive provenance; any content change invalidates the digest. HMAC authentication does not decrypt or sanitize an archive.
- Legacy migration unexpectedly wants to write: omit `--apply`; previews are the default. `--adopt` is a separate explicit decision to retain the recorded resume identity.

Linux systemd-user and Windows Task Scheduler behavior is generator-tested only. A generated definition is not evidence that a native service, restart, logout, or reboot path works on that host.
