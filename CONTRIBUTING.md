# Contributing

## Plugin contract

Each `plugins/<id>/` directory must be standalone and contain its own README, license, version, tests, package/build metadata where applicable, and self-contained Hermes plugin directory with `plugin.yaml` and registration entry point. Runtime code must not depend on the monorepo root or sibling plugins.

Add an entry to `plugins.json`; keep its id equal to the directory name and use repository-relative paths. Follow official `hermes-example-plugins` manifest conventions: a stable `name`, version, quoted description, author, hooks, and declared `provides` surfaces.

## Development

Use strict RED-GREEN-REFACTOR for behavior and path changes. From any directory:

```sh
/path/to/hermes-plugins/scripts/plugins all
```

Before a pull request, verify a clean build from each affected plugin root, run all plugin tests and E2E tests, and run `hermes plugins doctor <plugin-path> --ci`. Never use live profile installation, active OMP sessions, gateway state, or secrets as test fixtures.

## Commits

Use Conventional Commits. Keep plugin changes independently reviewable and update documentation/catalog metadata in the same commit.
