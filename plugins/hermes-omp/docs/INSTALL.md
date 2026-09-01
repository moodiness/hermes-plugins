# Installation and uninstall

Requirements: Python 3.9+, Hermes Agent 0.20.6+, OMP 18.0.10+. Build/install the wheel, then run `hermes plugins install ./plugin`. Verify with `hermes omp doctor --json`.

The service backend installs a user LaunchAgent on macOS, a `systemd --user` unit on Linux, or a per-user Task Scheduler entry on Windows. RC1 is host-validated only on macOS.

Uninstall: stop and remove every managed session, run `hermes plugins uninstall hermes_omp`, uninstall the Python package, then remove `$HERMES_HOME/omp` only after retaining any desired logs/state. Removal never touches OMP sessions not owned by this plugin.
