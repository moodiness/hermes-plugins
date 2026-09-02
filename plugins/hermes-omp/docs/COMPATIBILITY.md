# Compatibility and versioning

This matrix applies to hermes-omp `0.3.0rc1`.

| Component | Verified or tested scope | Limit |
|---|---|---|
| Hermes Agent | 0.21.0, tag `v2026.8.31`, commit `29112bef099274229cadff79cdff7bf7b99c4b77` | Wider Hermes versions are unverified; no broader minimum is claimed |
| Hermes host Python | `>=3.11,<3.14` | This is Hermes's interpreter range |
| Declared standalone package CI matrix | Python 3.10, 3.11, and 3.13 on Linux, macOS, and Windows | This cross-OS matrix is configured but was not executed by the local validation; Python 3.10 does not make it a supported Hermes host interpreter |
| OMP | 18.0.10 baseline | RPC behavior is exercised with a deterministic fake; wider native OMP compatibility is unverified |
| Editable development install | pip 21.3+ | PEP 660 support is required by Hatchling |
| macOS launchd | Definition and injected-runner tests; local fake-process runtime | No real active service, restart, logout, or reboot test |
| Linux systemd-user | Definition and injected-runner tests | No native systemd host validation |
| Windows Task Scheduler | XML and injected-runner tests | No native Task Scheduler host validation |

Subprocess E2E uses a temporary `HERMES_HOME`, fake OMP, fake Hermes delivery, and injected service runners. Exact-Hermes plugin discovery/doctor validation is a separate compatibility check; neither category exercises a real gateway, channel credential, active service, restart, or reboot.

Windows Task Scheduler has no direct equivalent of systemd's unlimited `Restart=always`: `never` omits restart settings, while both `on-failure` and `always` emit `RestartOnFailure` with `Count=999`. Task Scheduler applies that setting only after failures, so a clean exit is not forced to restart by the generated XML. The large native count is not the session's policy budget: hermes-omp's serialized, persistent restart budget is the enforcement authority and exits successfully after refusing a launch so failure-only manager retries stop. XML and injected-runner tests cover this mapping; no native Task Scheduler restart behavior is claimed as validated.

State migrations are forward-only and reject unknown newer schemas. The supported-version policy beyond the verified baselines remains deferred.
