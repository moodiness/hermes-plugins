# Configuration

All session settings are explicit `hermes omp create` flags: cwd/project, model, OMP options, destination platform/chat/topic, allowed users, mission, and restart policy. State is beneath `$HERMES_HOME/omp`; no secret configuration exists. Channel credentials remain exclusively managed by Hermes.

The test-only `HERMES_OMP_BINARY` and `HERMES_OMP_HERMES` process variables inject deterministic executables. They are not user credential settings.

## Inbound interface

Hermes 0.20.6 exposes public plugin CLI registration and outbound `hermes send`, but no documented generic inbound message hook for third-party standalone runtimes. The replaceable adapter accepts atomically written JSON envelopes at `$HERMES_HOME/omp/inbox/NAME/EVENT.json` with `event_id`, `question_id`, `platform`, `chat`, `topic`, `user`, and `answer`.

Proposed upstream hook: `ctx.register_inbound_message_handler(handler)` where the immutable envelope carries those routing/sender fields and handler acknowledgement controls replay. This is generic, opt-in, and avoids gateway internals. Until available, an authorized webhook/adapter invokes `hermes omp inbound ...`; it must never expose channel credentials.
