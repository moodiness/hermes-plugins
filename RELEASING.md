# Release and versioning policy

Plugins are versioned and released independently using Semantic Versioning. A monorepo-wide version does not exist.

- The canonical version is the nested plugin's package/manifest version; all copies must agree.
- Stable tags use `<plugin-id>-vX.Y.Z`; prereleases use `<plugin-id>-vX.Y.Z-rc.N`.
- A release changes only the affected plugin, its changelog, and its `plugins.json` entry.
- Build wheel/sdist from the nested plugin directory, test the installed artifacts in a fresh supported-Python environment, run plugin doctor with `--ci`, and publish SHA-256 checksums.
- CI must pass on all declared supported Python/OS combinations before tagging.
- Breaking manifest, CLI, state, or archive changes require a major version or an explicit prerelease migration path.

Historical import provenance is recorded in `plugins.json`. Subsequent monorepo history is authoritative.
