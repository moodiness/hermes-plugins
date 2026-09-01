# Compatibility and versioning

| Component | Minimum | RC validation |
|---|---:|---|
| Hermes Agent | 0.20.6 | macOS local CLI/plugin doctor |
| OMP | 18.0.10 | RPC contract via deterministic fake; installed version detected |
| Python | 3.9 | local 3.9 and CI matrix |
| macOS launchd | macOS 26.6 | generator/runtime locally tested; no real active service touched |
| Linux systemd-user | modern systemd | generator + CI unit tests only |
| Windows Task Scheduler | Windows 2022+ | XML generator + CI unit tests only |

Semantic Versioning is used. State schema migrations are forward-only, atomic, and reject newer unknown versions. RC releases are local artifacts until explicitly approved.
