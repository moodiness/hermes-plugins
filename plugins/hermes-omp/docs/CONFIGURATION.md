# Configuration

Session settings are explicit `hermes omp create` flags: cwd/project, model, OMP options, destination platform/chat/topic, allowed users, mission, resume id, executable path, and restart policy. `hermes omp update` changes only its mutable session fields; it is not a package or plugin upgrade.

State lives at `<active-HERMES_HOME>/omp`. Generated services carry that exact directory as the internal runtime's required `--root` argument instead of rediscovering it from a service manager's ambient profile. They also carry `--expected-session-id`; the runtime compares it to the loaded record before acquiring ownership. This is an identity handoff and state-race check, not OS-level process identity proof.

## Environment behavior

The OMP child and outbound Hermes CLI inherit the complete environment available to the supervisor. The outbound bridge overwrites its child `HERMES_HOME` with the profile derived from explicit `--root`; other variables remain inherited. Service-manager launch environments can differ from an interactive shell. The launchd definition captures `HERMES_HOME` and `PATH` when generated, while the explicit root remains authoritative for plugin state.

These production-effective variables override normal selection:

- `HERMES_HOME`: profile used by operator CLI discovery.
- `HERMES_OMP_BINARY`: OMP executable, overriding a session's stored `--omp-path` at runtime.
- `HERMES_OMP_HERMES`: executable used for version checks and outbound `hermes send`.
- `HERMES_OMP_AUTO_ANSWER_SAFE=1`: enables heuristic automatic answers classified as safe.

They are operational inputs, not credential settings, test-only hooks, allowlists, or security boundaries. Environment filtering/allowlisting is deliberately deferred.

## Inbound interface

Hermes 0.21.0 has no documented generic inbound message hook for this standalone runtime. An authorized adapter may call `hermes omp inbound NAME --event-id ID --question-id ID --platform PLATFORM --chat CHAT --topic TOPIC --user USER --answer ANSWER`; the command atomically queues an envelope beneath `$HERMES_HOME/omp/inbox/NAME`. Runtime authorization, correlation, expiry, and answer validation occur later. The adapter must not expose channel credentials.
