# Contributing

## Plugin contract

Each `plugins/<id>/` directory must be standalone and contain its own README, license, version, tests, package/build metadata where applicable, and self-contained Hermes plugin directory with `plugin.yaml` and registration entry point. Runtime code must not depend on the monorepo root or sibling plugins.

Add an entry to `plugins.json`; keep its id equal to the directory name and use repository-relative paths. Follow official `hermes-example-plugins` manifest conventions: a stable `name`, version, quoted description, author, hooks, and declared `provides` surfaces.

## Development

Use strict RED-GREEN-REFACTOR for behavior and path changes. Run repository-relative commands from the repository root; from elsewhere, invoke the script by absolute path:

```sh
/path/to/hermes-plugins/scripts/plugins all
```

Before a pull request, verify a clean wheel and sdist build from each affected plugin root, run all plugin tests and E2E tests, and run `hermes plugins doctor <source-or-copied-plugin-path> --ci` against the installable plugin directory. Manual hermes-omp installation remains a two-artifact operation: install the wheel into the isolated Python environment that supplies Hermes and copy the plugin directory into the active profile as `omp`. Run directory doctor before enabling trusted code and `hermes omp doctor --json` afterward. Never use live profile installation, active OMP sessions, gateway state, or secrets as test fixtures.

## Commits

Use Conventional Commits. Keep plugin changes independently reviewable and update documentation/catalog metadata in the same commit.
