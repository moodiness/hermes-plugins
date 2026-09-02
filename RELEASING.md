# Release and versioning policy

Plugins are versioned and released independently using Semantic Versioning. A monorepo-wide version does not exist.

- The canonical version is the nested plugin's package/manifest version; all copies must agree.
- Stable tags use `<plugin-id>-vX.Y.Z`; prereleases use `<plugin-id>-vX.Y.Z-rc.N`.
- A release changes only the affected plugin, its changelog, and its `plugins.json` entry.
- Build the wheel and sdist from the nested plugin directory, test both release distributions in a fresh supported-Python environment, run `hermes plugins doctor <source-or-copied-plugin-path> --ci` against the installable plugin directory before enabling it, and publish SHA-256 checksums.
- Preserve the two-artifact installation model: the wheel is installed into the same isolated Python environment as Hermes, while the plugin directory is copied separately into the active profile under its plugin id. After enabling the plugin, verify it with its operational doctor, such as `hermes omp doctor --json` for hermes-omp.
- CI must pass on all declared supported Python/OS combinations before tagging.
- Breaking manifest, CLI, state, or archive changes require a major version or an explicit prerelease migration path.

Historical import provenance is recorded in `plugins.json`: `source_commit` is the final commit from the pre-monorepo source repository at import time, not the current monorepo revision. Subsequent monorepo history is authoritative.
