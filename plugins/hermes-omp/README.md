# hermes-omp

Standalone Hermes Agent plugin for durable Oh My Pi (OMP) RPC sessions. It keeps OMP independent from Hermes Desktop/gateway, persists session and delivery state beneath `HERMES_HOME/omp`, and uses only the public `hermes send` CLI for outbound delivery.

## Status

`0.1.0rc1`. macOS runtime and LaunchAgent behavior are validated locally. Linux systemd-user and Windows Task Scheduler definitions are unit-tested generators, not host-tested in this release candidate.

## Install

```sh
python -m pip install dist/hermes_omp-0.1.0rc1-py3-none-any.whl
hermes plugins install ./plugin
hermes omp doctor --json
```

The plugin directory contains `plugin.yaml` and registers `hermes omp` through `ctx.register_cli_command`; it does not patch Hermes core.

## Quick start

```sh
hermes omp create work --cwd ./project --model gpt-5.6-sol-pro --mission "Implement the change" --platform telegram --chat CHAT --topic TOPIC --allowed-user USER --omp-path "$(command -v omp)"
hermes omp status work --json
hermes omp send work "Run the focused tests"
hermes omp logs work --lines 100
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
