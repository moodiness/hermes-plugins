# hermes-omp

Standalone Hermes Agent plugin for durable Oh My Pi (OMP) RPC sessions. It keeps OMP independent from Hermes Desktop/gateway, persists session and delivery state beneath `HERMES_HOME/omp`, and uses only the public `hermes send` CLI for outbound delivery.

## Status

`0.2.0rc1`. macOS runtime and LaunchAgent behavior are validated locally. Linux systemd-user and Windows Task Scheduler definitions are unit-tested generators, not host-tested in this release candidate.

## Install

```sh
python -m pip install plugins/hermes-omp/dist/hermes_omp-0.2.0rc1-py3-none-any.whl
cp -r plugins/hermes-omp/plugin ~/.hermes/plugins/omp  # from hermes-plugins monorepo root
hermes plugins enable omp
hermes omp doctor --json
```

This checkout lives at `plugins/hermes-omp/` in the `hermes-plugins` monorepo. Paths in plugin metadata and CI are plugin-root-relative; neither runtime nor tests assume the monorepo root is the package root. The self-contained `plugin/` directory contains `plugin.yaml` and `__init__.py`. Copy it from the plugin root into the active profile's user-plugin directory, enable `omp`, then run doctor. It registers `hermes omp` through the documented public `ctx.register_cli_command` surface; the official examples currently demonstrate slash-command/LLM/dashboard surfaces rather than this CLI surface, so no core patching or private imports are used.

Its manifest follows official example conventions (`name`, quoted description, author, hooks, and declared `provides`), registration is typed, and command dispatch uses module logging for auditable start/failure/finish events without arguments, message bodies, routes, or secrets.

## Quick start

```sh
hermes omp create work --cwd ./project --model gpt-5.6-sol-pro --mission "Implement the change" --platform telegram --chat CHAT --topic TOPIC --allowed-user USER --omp-path "$(command -v omp)"
hermes omp status work --json
hermes omp send work "Run the focused tests"
hermes omp logs work --lines 100
hermes omp events work --queue outbound,inbound --status dead,rejected --json
hermes omp export work work.json --json
hermes omp stop work
```

Never put credentials in these commands. Hermes owns channel credentials. See `docs/INSTALL.md`, `docs/CONFIGURATION.md`, `docs/CLI.md`, `docs/SECURITY.md`, and `docs/RECOVERY.md`.

## Architecture

- `plugin/`: standalone Hermes manifest and public CLI registration.
- `src/hermes_omp/runtime.py`: independent OMP RPC supervisor.
- `core.py`: schema-v2 atomic state, correlation, authorization, redaction, FIFO outbox/dead letters.
- `bridge.py`: outbound `hermes send --file -`; replaceable atomic JSON inbound contract.
- `service.py`: launchd implementation and portable service definition backends.
- `skills/omp-service/SKILL.md`: agent operating procedure.

No code reads `state.db`, imports gateway internals, or calls Telegram APIs.

## 0.2 operations

Every user command supports `--json`; failures use stable exit codes: `1` operational, `2` usage, `3` not found, `4` conflict, and `5` validation. `events` inspects redacted prompt/outbound/inbound queues, `retry` explicitly requeues dead outbound items, and `status`/`list` report health, queue depths, activity, and last error. Portable versioned archives exclude secrets, PID state, and owner locks. `import` supports `fail`, `rename`, and `replace` conflicts with dry-run and rollback. `update` accepts mutable model/options/destination/allowed-user/mission/restart-policy fields; live updates require `--apply-restart`. Create/adopt, import, update, and doctor provide dry-run modes. Standalone bash/zsh/fish completions are available with `hermes-omp completion SHELL`.

## Delivery semantics

Outbound events have stable IDs and durable FIFO at-least-once delivery. Successful `hermes send` calls are acknowledged locally. A crash after remote acceptance but before local acknowledgement may duplicate one event; receivers should deduplicate by the stable event ID included in question text. Exponential backoff has jitter, and exhausted events enter the dead-letter state.

## Development

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade 'pip>=21.3'
python -m pip install -e '.[dev]'
pytest -q
python -m build
```

The pip bootstrap is required because Python 3.9 may create a venv with pip 21.2.4, while Hatchling editable installs use PEP 660 support introduced in pip 21.3. Use `\.venv\Scripts\activate` on Windows.

See `artifacts/` for recorded RED/GREEN and release verification evidence.
