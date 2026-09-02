# Migration from omp-service

Do not migrate an active session in place. Record cwd, model, route, mission, and the exact OMP `--resume` id without copying credentials. Produce trusted inspection JSON containing the inspected `argv` and `cwd`; this file is an operator assertion, not OS process-identity proof.

Preview without persisting or installing a service:

```sh
hermes omp adopt NAME --inspection FILE --mission TEXT --platform PLATFORM --chat CHAT --topic TOPIC --allowed-user USER --dry-run --json
```

Then stop the legacy supervisor, independently verify that its child has exited, and perform the adoption and user-service start:

```sh
hermes omp adopt NAME --inspection FILE --mission TEXT --platform PLATFORM --chat CHAT --topic TOPIC --allowed-user USER --start --json
hermes omp status NAME --json
```

The generated service carries explicit profile `--root` and `--expected-session-id`; confirm the expected route, resume id, and exactly one heuristic owner before accepting traffic. Keep legacy state read-only until completion. To roll back, run `hermes omp stop NAME`, confirm `active` is false with `hermes omp status NAME --json`, and only then restart the legacy owner.
