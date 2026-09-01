# Migration from omp-service

Do not migrate an active session in place. Record its cwd, model, route, mission and explicit OMP `--resume` ID without copying secrets. Stop the legacy supervisor, verify its OMP child exited, create a trusted inspection JSON, run `hermes omp adopt ... --no-install`, compare status, install/start the new service, then verify exactly one owner. Keep legacy state read-only until completion. Roll back by stopping hermes-omp before restarting the legacy owner.
