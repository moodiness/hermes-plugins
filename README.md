# hermes-plugins

Independent monorepo for production-shaped Hermes Agent plugins. Every directory under `plugins/` is autonomous: it owns its package metadata, tests, documentation, build artifacts, and installable Hermes plugin directory.

## Catalog

| Plugin | Version | Purpose |
|---|---:|---|
| [`hermes-omp`](plugins/hermes-omp) | 0.2.0rc1 | Durable, independent OMP RPC supervision |

Machine-readable metadata lives in [`plugins.json`](plugins.json).

## Repository commands

Commands resolve the repository from their own location, so they work from any current directory:

```sh
./scripts/plugins list
./scripts/plugins test
./scripts/plugins build
./scripts/plugins doctor
./scripts/plugins all
```

`test`, `build`, and `doctor` may also take one plugin id. Test and build commands create/reuse an ignored `.venv-verify` in each plugin, upgrade it to `pip>=21.3`, and install that plugin's `.[dev]` extra. Set `PLUGIN_VERIFY_PYTHON=/path/to/python` to choose the base Python used to create verification environments and read the catalog. Builds preserve tracked release artifacts, add freshly built distributions, then deterministically refresh and verify `dist/SHA256SUMS`. Doctor validates the plugin directory without installing it.

## Install a plugin

Follow the nested plugin's README. Do not install the monorepo root as a Python package. For hermes-omp, the Python distribution root is `plugins/hermes-omp`, while the directory accepted by Hermes is `plugins/hermes-omp/plugin`.

## Safety

Repository verification does not enable or install plugins into a live Hermes profile, start OMP sessions, touch gateways, or read credentials.

## License

MIT. See [`LICENSE`](LICENSE).
