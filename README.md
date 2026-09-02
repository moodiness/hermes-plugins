# hermes-plugins

Independent monorepo for production-shaped Hermes Agent plugins. Every directory under `plugins/` is autonomous: it owns its package metadata, tests, documentation, release distributions, and installable Hermes plugin directory.

## Catalog

| Plugin | Version | Purpose |
|---|---:|---|
| [`hermes-omp`](plugins/hermes-omp) | 0.3.0rc1 | Durable, independent OMP RPC supervision |

Machine-readable metadata lives in [`plugins.json`](plugins.json).

## Repository commands

Run these repository-relative commands from the repository root:

```sh
./scripts/plugins list
./scripts/plugins test
./scripts/plugins build
./scripts/plugins doctor
./scripts/plugins all
```

From another current directory, invoke the script by absolute path, for example `/absolute/path/to/hermes-plugins/scripts/plugins all`. The script then resolves the repository from its own location.

`test`, `build`, and `doctor` may also take one plugin id. Test and build commands create/reuse an ignored `.venv-verify` in each plugin, upgrade it to `pip>=21.3`, and install that plugin's `.[dev]` extra. Set `PLUGIN_VERIFY_PYTHON=/path/to/python` to choose the base Python used to create verification environments and read the catalog. Builds preserve tracked release artifacts, add freshly built distributions, then deterministically refresh and verify `dist/SHA256SUMS`. Doctor validates the plugin directory without installing it.

## Install a plugin

Follow the nested plugin's README. Do not install the monorepo root as a Python package. A manual hermes-omp installation has two parts: install its wheel into the same isolated Python environment that supplies Hermes, then copy `plugins/hermes-omp/plugin` into the active profile's plugin root as `omp`. Run `hermes plugins doctor <source-or-copied-plugin-path> --ci` before enabling `omp`, then run `hermes omp doctor --json` as the operational check.

Hermes 0.21.0 does not support `hermes plugins install ./plugin` for this manual layout. To uninstall, disable and remove plugin id `omp`, then uninstall the `hermes-omp` distribution from the same isolated Python environment.

## Safety

Repository verification does not enable or install plugins into a live Hermes profile, start OMP sessions, touch gateways, or read credentials.

## License

MIT. See [`LICENSE`](LICENSE).
