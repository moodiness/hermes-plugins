# Installation and uninstall

The verified Hermes baseline is 0.21.0 (tag `v2026.8.31`, commit `29112bef099274229cadff79cdff7bf7b99c4b77`) on Python `>=3.11,<3.14`. OMP 18.0.10 is the exercised protocol baseline. Activate the same isolated Python environment that supplies Hermes and select the intended profile.

From the plugin root in a POSIX shell:

```sh
test -n "${HERMES_HOME:-}" || { echo "HERMES_HOME is required" >&2; exit 1; }
python -m pip install dist/hermes_omp-0.2.0rc1-py3-none-any.whl
mkdir -p "$HERMES_HOME/plugins"
test ! -e "$HERMES_HOME/plugins/omp" || { echo "plugin destination already exists" >&2; exit 1; }
cp -R plugin "$HERMES_HOME/plugins/omp"
hermes plugins doctor "$HERMES_HOME/plugins/omp" --ci
hermes plugins enable omp
hermes omp doctor --json
```

From the plugin root in native Windows PowerShell:

```powershell
if (-not $env:HERMES_HOME) { throw "HERMES_HOME is required" }
python -m pip install dist/hermes_omp-0.2.0rc1-py3-none-any.whl
$destination = Join-Path $env:HERMES_HOME "plugins/omp"
if (Test-Path -LiteralPath $destination) { throw "plugin destination already exists" }
New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
Copy-Item -Recurse -LiteralPath plugin -Destination $destination
hermes plugins doctor $destination --ci
hermes plugins enable omp
hermes omp doctor --json
```

The destination directory must not already exist; both command blocks enforce this before copying. The wheel supplies the `hermes-omp` distribution and runtime; the copied directory supplies native plugin id `omp`. `~/.hermes` is only the default profile. Hermes 0.21.0 does not accept a local directory through `hermes plugins install ./plugin`, so manual copy is canonical. The directory doctor validates the native plugin before trusted code is enabled; the operational doctor then checks runtime dependencies and profile state.

Session services are user-scoped: launchd on macOS, systemd-user on Linux, and Task Scheduler on Windows. Each generated service passes the selected state directory explicitly as runtime `--root` and the persisted identity as `--expected-session-id`. Only macOS generator/runtime behavior has been locally exercised, without a real active service; Linux and Windows native managers, restart, logout, and reboot behavior have not been validated.

`hermes omp update NAME ...` edits session configuration only. Package/plugin replacement remains a manual lifecycle: stop sessions, disable `omp`, replace the reviewed wheel and plugin directory in the same profile/environment, run the directory doctor, re-enable `omp`, and run the operational doctor. There is no documented managed upgrade command for this installation form.

For uninstall, stop and remove each managed session before removing the plugin and distribution:

```sh
hermes omp stop NAME
hermes omp status NAME --json
hermes omp remove NAME
hermes plugins disable omp
hermes plugins uninstall omp
python -m pip uninstall hermes-omp
```

Confirm `active` is false before `remove`. Retain any required logs/state before deleting `$HERMES_HOME/omp`. These steps address plugin-owned state only; they do not identify or remove unrelated OMP processes.
