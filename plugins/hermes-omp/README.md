# hermes-omp

Standalone Hermes plugin for durable Oh My Pi (OMP) RPC sessions. The Python distribution is `hermes-omp`, the native Hermes plugin id is `omp`, and each operator-chosen session name is a separate identity.

## Verified scope

Version `0.3.0rc1` targets Hermes Agent 0.21.0 (tag `v2026.8.31`, commit `29112bef099274229cadff79cdff7bf7b99c4b77`). Hermes runs on Python `>=3.11,<3.14`; the standalone package declares Python `>=3.9` and its CI matrix includes 3.9, 3.11, and 3.13, but this local validation does not establish that cross-OS matrix. Wider Hermes compatibility is not established.

Runtime behavior has been exercised locally on macOS; launchd, systemd-user, and Windows Task Scheduler definitions and manager calls are covered with injected runners, not real active services or native managers. Subprocess E2E uses temporary state plus fake OMP and fake Hermes executables; it does not exercise a real gateway, channel, service manager, restart, or reboot.

## Manual installation

Activate the same isolated Python environment that supplies `hermes`, set `HERMES_HOME` to the intended profile, and run these POSIX-shell commands from this plugin directory:

In this monorepo the plugin root is `plugins/hermes-omp`, so the native directory is `plugins/hermes-omp/plugin` from the repository root and `plugin` from this plugin root.

```sh
test -n "${HERMES_HOME:-}" || { echo "HERMES_HOME is required" >&2; exit 1; }
python -m pip install dist/hermes_omp-0.3.0rc1-py3-none-any.whl
mkdir -p "$HERMES_HOME/plugins"
test ! -e "$HERMES_HOME/plugins/omp" || { echo "plugin destination already exists" >&2; exit 1; }
cp -R plugin "$HERMES_HOME/plugins/omp"
hermes plugins doctor "$HERMES_HOME/plugins/omp" --ci
hermes plugins enable omp
hermes omp doctor --json
```

The destination `omp` directory must not already exist. `~/.hermes` is only Hermes's default profile; an explicit `HERMES_HOME` selects another profile. Hermes 0.21.0 does not support `hermes plugins install ./plugin`; installing the wheel and manually copying the native plugin directory is the canonical two-part flow. `register(ctx)` is the authoritative CLI registration; the manifest does not declare a recognized CLI inventory field.
For native Windows PowerShell, use the guarded commands in [Installation](docs/INSTALL.md); do not translate `$HERMES_HOME`, `mkdir -p`, or `cp -R` literally.

See [Installation](docs/INSTALL.md) for replacement and uninstall instructions.

## Quick start

```sh
hermes omp create work --cwd ./project --model gpt-5.6-sol-pro --mission "Implement the change" --platform telegram --chat CHAT --topic TOPIC --allowed-user USER --omp-path "$(command -v omp)"
hermes omp status work --json
hermes omp send work "Run the focused tests"
hermes omp logs work --lines 100
hermes omp events work --queue outbound,inbound --status dead,rejected --json
hermes omp export work work.json --json
hermes omp stop work
hermes omp clone work experiment --no-install --json
hermes omp watch work --json
hermes omp diagnose work --output work-diagnosis.json --json
hermes omp migrate-legacy migrated --source reviewed-legacy.json --json

```

Never place credentials in command arguments. Hermes owns channel credentials.

## Operational boundaries

- State is stored under the selected profile's `omp` directory. Generated services pass that directory through the internal runtime's explicit `--root` argument and pass `--expected-session-id`; startup rejects a replaced session record rather than adopting its identity.
- On POSIX, OMP starts in a new process group and cleanup targets that exact group. On Windows, cleanup targets the direct child process. PID/PGID liveness is a reuse-prone heuristic, not proof of executable identity.
- The supervisor's complete environment is inherited by child processes. `HERMES_OMP_BINARY`, `HERMES_OMP_HERMES`, and `HERMES_OMP_AUTO_ANSWER_SAFE=1` are production-effective overrides, not test-only settings or security boundaries. The outbound bridge forces its child `HERMES_HOME` to the selected profile.
- Outbound delivery uses `hermes send --to TARGET --file - --quiet`. Delivery is FIFO and at least once; a crash after remote acceptance but before local acknowledgement can duplicate an event.
- Export files omit live PID fields and owner locks and apply heuristic redaction, but still contain session configuration and may contain sensitive paths, prompts, options, executable locations, or unrecognized secrets. Optional HMAC-SHA256 authentication detects modification but does not encrypt content; pass keys only through `--hmac-key-file` or `--hmac-key-env` references.
- `hermes omp update` changes stored session configuration. It does not upgrade the `hermes-omp` distribution or replace the native `omp` plugin directory.
- Approval profiles are stored with each session. `balanced` and `night` can automatically answer only choices already classified safe, recommended, and reversible; `interactive` and `strict` require explicit handling. Recognized sensitive actions are never automatic.
- Notification controls are per kind: question, error, milestone, completion, and restart. Use repeatable `--no-notify KIND` on create; deduplication fingerprints persist across supervisor restarts.
- Duration and restart-window/cooldown budgets are enforced locally. The initial launch is free; every later supervisor launch is a restart. Launch claims are serialized with session ownership and persisted before OMP starts, so manager-driven and concurrent invocations cannot bypass the limit. Refused launches exit successfully with stored status `restart_budget_exceeded`; failure-only service policies therefore stop retrying. Token/cost caps fail closed because this release has no trustworthy documented public OMP usage RPC; status reports them as unavailable rather than estimating usage.
- Transition records are bounded, redacted local NDJSON at `$HERMES_HOME/omp/logs/NAME.transitions.ndjson`. Oversized records are replaced by a valid truncated record rather than emptying the file. No telemetry is emitted.
- `migrate-legacy` reads only an explicit reviewed JSON record or documented profile-local candidates. It never inspects or stops a legacy process, and it writes nothing unless `--apply` is present.

## Desktop dashboard

The unified plugin includes `plugin/desktop/plugin.js` and `plugin/dashboard/plugin_api.py` using the documented public Desktop Plugin SDK. Enable both the Python plugin and the opt-in Desktop half in Settings → Plugins. The OMP page is read-only by default and shows sessions, health, pending questions, and bounded redacted log summaries. Any action opens a confirmation dialog and can only request a validated `hermes omp` CLI contract; the backend does not execute subprocesses.

## Development

From this plugin directory:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade 'pip==25.2'
python -m pip install -e '.[dev]'
pytest -q
python -m pip install -r requirements-build.txt
sh scripts/build-release.sh
```

Use `.venv\Scripts\activate` on Windows. Relative paths above are plugin-root-relative; use absolute paths when invoking them elsewhere. Files under `artifacts/` are historical transcripts, not fresh proof for the current checkout.
