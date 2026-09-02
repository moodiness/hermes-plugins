# Migration from omp-service

Legacy migration is file-based and never process-based. Preserve a reviewed JSON record containing cwd, model, mission, route, options, restart policy, executable path, and any known OMP resume id. Do not read credentials into it and do not migrate an active owner in place.

Preview the mapping; this is the default and writes no session or service:

```sh
hermes omp migrate-legacy NAME --source REVIEWED.json --no-install --json
```

After stopping the legacy supervisor independently and verifying its child exited, persist the new configuration. Add `--adopt` only when the reviewed resume id must be retained:

```sh
hermes omp migrate-legacy NAME --source REVIEWED.json --apply --adopt --start --json
hermes omp status NAME --json
```

For a live process whose arguments were inspected by an operator, the existing `adopt` workflow remains available: produce trusted inspection JSON containing `argv` and `cwd`, preview with `hermes omp adopt NAME --inspection FILE --mission TEXT --dry-run --json`, then stop the source independently before applying. Neither workflow discovers, signals, or terminates the source process.
