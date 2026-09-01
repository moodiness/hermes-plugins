# Hermes OMP Safe Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair hermes-omp’s existing durability, lifecycle, packaging, CI, and documentation contracts and verify them against Hermes Agent 0.21.0 without changing deferred product or security policy.

**Architecture:** Keep the current split between the `hermes-omp` Python distribution and copied native plugin directory. Harden the existing JSON state/queue architecture with transactional commit order and cross-process locking, share one public argparse builder between console entry points, and validate all host integration through temporary profiles and injected/fake processes.

**Tech Stack:** Python 3.13, stdlib `argparse`/`subprocess`/`selectors`/`fcntl`/`msvcrt`, pytest, Hatchling, GitHub Actions, Hermes Agent 0.21.0.

**Spec:** `docs/superpowers/specs/2026-09-01-hermes-omp-safe-hardening-design.md`

## Global Constraints

- Work only in `/tmp/hermes-plugins-hermes-omp-audit` on branch `audit/hermes-omp-hardening`.
- Never install or enable anything in a real Hermes profile.
- Never start, stop, inspect, or signal an existing OMP session or Hermes gateway.
- Never read real credentials or secrets.
- Never install a real LaunchAgent, systemd user unit, or Windows Scheduled Task on the workstation.
- Never push, publish, tag, open a PR, merge, or deploy.
- Preserve the existing two-artifact installation model.
- Preserve public CLI names and state/archive schema versions.
- Do not change empty-allowlist authorization, subprocess environment policy, archive content/executable policy, `status` exit semantics, supported-version range, skill distribution, CI Hermes acquisition, dependency locking, CI matrix size, signing, or provenance.
- Every production behavior change requires a demonstrated failing regression test before implementation.
- Use only temporary `HERMES_HOME` directories and plugin-local virtual environments.

---

### Task 1: Transactional RPC responses and stream framing

**Files:**
- Modify: `plugins/hermes-omp/src/hermes_omp/runtime.py`
- Modify: `plugins/hermes-omp/tests/test_runtime.py`

**Interfaces:**
- Produces: `InboundResult(response=None, retryable=False, terminal=False, question_id="")`.
- Produces: `Runtime.commit_response(question_id: str, event_id: str = "", now: float | None = None) -> None`.
- Produces: `RpcLineBuffer.feed(data: bytes) -> list[str]` and `RpcLineBuffer.finish() -> str`.
- Preserves: `Runtime.accept_inbound(...)` public call shape and OMP JSON frame schema.

- [ ] **Step 1: Add failing inbound commit-order tests**

Add tests that construct a real `Runtime`, create one question, accept an authorized event, and assert the question and event replay state remain pending until `commit_response` is called:

```python
result = runtime.accept_inbound(event, now=2)
assert result.response == {"type": "extension_ui_response", "id": "q", "value": "A"}
assert result.question_id == "q"
assert runtime.question is not None
assert "e" not in runtime.seen
runtime.commit_response(result.question_id, "e", now=3)
assert runtime.question is None
assert "e" in runtime.seen
```

Add two supervisor-level integrations around `run`, using a controlled fake child and real inbox files. In the failure case, the child reads startup frames, emits a question, closes its stdin, and remains alive long enough for the supervisor write to raise; assert the durable question and original inbox event remain pending. In the success case, the child records a complete newline-terminated response before exiting; assert the question is cleared and the inbox file moves to `processed` only after that observation. Both tests must own bounded `finally` cleanup for their fake child. Update the automatic-answer test to assert `on_event` returns an RPC while the question remains pending until commit.

- [ ] **Step 2: Run inbound tests and verify RED**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_runtime.py -k 'commit or transactional_response or auto_answers'
```

Expected: failures because `InboundResult` has no `question_id`, accepted events are committed immediately, automatic answers clear state before the write, or `run` does not preserve inbox/state on the failed flush.

- [ ] **Step 3: Implement prepare/write/commit state transitions**

Change `accept_inbound` so valid answers return a prepared response without mutating `seen`, `question`, or activity. Add `commit_response` that validates the current question ID, records a non-empty event ID, clears the question, saves runtime state, removes the public pending-question file, and updates activity. Keep terminal invalid/unauthorized events terminal because they require no OMP write.

Change automatic handling so `on_event` returns `{"rpc": frame, "question_id": id}` without clearing state. In `run`, write and flush first, then call `commit_response`; on `BrokenPipeError`/`OSError`, do not commit or acknowledge the inbox event.

- [ ] **Step 4: Add failing fragmented-stream tests**

Add literal UTF-8 JSONL cases:

```python
buffer = RpcLineBuffer()
assert buffer.feed(b'{"type":"message') == []
assert buffer.feed(b'_end"}\n{"type":"turn_end"}\npart') == [
    '{"type":"message_end"}',
    '{"type":"turn_end"}',
]
assert buffer.finish() == "part"
```

Include a multibyte character split between byte chunks and assert the reconstructed line contains the original character.

- [ ] **Step 5: Run framing tests and verify RED**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_runtime.py -k 'fragmented or multibyte'
```

Expected: collection failure because `RpcLineBuffer` does not exist.

- [ ] **Step 6: Implement incremental UTF-8 JSONL framing**

Use `codecs.getincrementaldecoder("utf-8")("replace")`. `feed` appends decoded text, emits complete lines in order, strips a trailing `\r`, and retains only the incomplete suffix. `finish` finalizes the decoder and returns the remaining text. Replace direct chunk `decode(...).splitlines(True)` in `run`; parse only emitted complete lines and log final residue as redacted unparsed content.

- [ ] **Step 7: Verify Task 1 GREEN**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_runtime.py tests/test_e2e.py
```

Expected: all selected tests pass with no leaked process.

- [ ] **Step 8: Commit Task 1**

```bash
git add plugins/hermes-omp/src/hermes_omp/runtime.py plugins/hermes-omp/tests/test_runtime.py
git commit -m "fix: commit OMP responses after RPC flush"
```

---

### Task 2: Supervisor-owned child cleanup and E2E teardown

**Files:**
- Modify: `plugins/hermes-omp/src/hermes_omp/runtime.py`
- Modify: `plugins/hermes-omp/tests/test_runtime.py`
- Modify: `plugins/hermes-omp/tests/test_e2e.py`
- Modify: `plugins/hermes-omp/tests/fixtures/fake_omp.py`

**Interfaces:**
- Produces: `_terminate_child(child: subprocess.Popen, timeout: float = 5.0) -> None` for children created by this supervisor only.
- Preserves: owner-lock format and `run(name, paths=...) -> int`.
- Consumes: Task 1 transactional response and `RpcLineBuffer` behavior.
- Removes: Task 1’s temporary post-exit time/byte truncation; owned-tree shutdown closes inherited descriptors before an unbounded drain-to-EOF of that now-closed tree.

- [ ] **Step 1: Add a failing real-process orphan regression**

Create a temporary executable Python fake that writes its PID to `FAKE_OMP_PID` and remains alive while reading stdin. Persist a session, put malformed JSON in its inbox before calling `run`, and assert:

```python
with pytest.raises(json.JSONDecodeError):
    run("demo", paths=paths)
wait_for(lambda: not _pid_alive(child_pid))
reloaded = SessionStore(paths).load("demo")
assert reloaded.status == "crashed"
assert reloaded.supervisor_pid == 0 and reloaded.omp_pid == 0
assert not (paths.run / "demo.owner").exists()
```

The test must own a `finally` cleanup that sends SIGTERM only to the PID written by its temporary fake, so the RED run cannot leak the known orphan. This also proves durable state no longer names the reaped child before ownership is released.
Add a POSIX-only owned-tree regression in which the direct fake exits after spawning a descendant that inherits stdout and would otherwise keep the selector open. Assert the supervisor terminates only that known process group, consumes every complete frame already emitted, reaches EOF without a time/byte truncation policy, reaps the direct child, and returns. Mark in-process pipe-selector integrations skipped on Windows because `selectors.DefaultSelector` cannot register subprocess pipes there; retain portable unit coverage and document native Windows runtime as unvalidated rather than pretending the test can execute.

- [ ] **Step 2: Run orphan test and verify RED**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_runtime.py -k orphan
```

Expected: failure because the owner lock disappears while the fake OMP PID remains live and durable session state still reports the exceptional run as active.

- [ ] **Step 3: Implement bounded child-tree cleanup**

On POSIX, terminate the process group whose id equals the child PID created with `start_new_session=True`, including surviving descendants even when the direct child has already exited; on Windows, terminate/kill the exact `Popen` child. Wait up to `timeout`, escalate only that known child/group, then reap the direct child. Invoke cleanup immediately after direct-child exit and before final stdout drain so inherited writers close and the existing incremental buffer can drain to EOF without arbitrary time or byte caps; invoke it again safely from `finally` for exceptions. In `run`’s `finally`, perform any remaining child cleanup first, then best-effort persist `status="crashed"` with cleared `supervisor_pid=0`/`omp_pid=0` when the normal terminal-state path was not reached, then release the owner lock. Cleanup/state-save failures must not mask the original runtime exception.

- [ ] **Step 4: Make subprocess E2E teardown unconditional**

Rename `test_all_16_isolated_acceptance_scenarios` to describe fake-process integration rather than claiming real restarts/reboots. Track every `Popen` created by the test and terminate/wait it in one `finally` block. Remove simulated Hermes/gateway marker files as evidence claims; retain observable queue outage/recovery, response correlation, resume identity, definition generation, and removal assertions.

Make `fake_omp.py` take its terminal delay from `FAKE_OMP_EXIT_DELAY` with a short deterministic default instead of a hard-coded two seconds.

- [ ] **Step 5: Verify Task 2 GREEN**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_runtime.py tests/test_e2e.py
```

Expected: all selected tests pass; no child process survives test teardown, inherited descriptors close through owned process-tree cleanup, and no RPC frame is truncated by an invented drain limit.

- [ ] **Step 6: Commit Task 2**

```bash
git add plugins/hermes-omp/src/hermes_omp/runtime.py plugins/hermes-omp/tests/test_runtime.py plugins/hermes-omp/tests/test_e2e.py plugins/hermes-omp/tests/fixtures/fake_omp.py
git commit -m "fix: reap supervised OMP children on every exit"
```

---

### Task 3: Serialized queues and exclusive session identity

**Files:**
- Modify: `plugins/hermes-omp/src/hermes_omp/core.py`
- Modify: `plugins/hermes-omp/src/hermes_omp/cli.py`
- Modify: `plugins/hermes-omp/tests/test_core.py`
- Modify: `plugins/hermes-omp/tests/test_cli.py`
- Modify: `plugins/hermes-omp/tests/test_release_features.py`

**Interfaces:**
- Produces: private `_path_lock(path: Path)` context manager using a per-path `threading.RLock` plus `fcntl.flock` or `msvcrt.locking`.
- Produces: `SessionStore.create(session: Session) -> None`, which raises `FileExistsError` on an existing local name and `ValueError` on a duplicate OMP session ID.
- Preserves: JSON session/outbox formats, existing `Outbox` method signatures, and public `.items` access as a fresh snapshot.

- [ ] **Step 1: Add deterministic stale-writer queue regressions**

Add tests using two `Outbox` instances created before either mutation:

```python
first = Outbox(path)
second = Outbox(path)
assert first.enqueue("a", {"n": 1})
assert second.enqueue("b", {"n": 2})
assert [item.id for item in Outbox(path).items] == ["a", "b"]
first.ack("a")
assert [(item.id, item.state) for item in Outbox(path).items] == [
    ("a", "delivered"),
    ("b", "pending"),
]
```

Add a stale `fail`/`retry` case proving an unrelated concurrently added item survives.
Add a stale-reader regression: create the reader before another instance enqueues, then assert the original instance’s `.items`, `pending()`, `due()`, and `dead_letters()` each observe the latest on-disk state.

- [ ] **Step 2: Run queue tests and verify RED**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_core.py -k 'stale or concurrent'
```

Expected: the second stale writer overwrites the first enqueue or later mutation, and a pre-existing reader misses newly persisted items.

- [ ] **Step 3: Implement locked reload-mutate-save and fresh reads**

For `enqueue`, `ack`, `fail`, and `retry`, acquire `_path_lock(self.path)`, reload the latest list from disk, apply the mutation, and call the existing atomic save before unlocking. Back `.items` with a private list and expose a read-only snapshot property that reloads under the same lock; make `pending`, `due`, and `dead_letters` consume one fresh snapshot per call. Keep FIFO ordering. Make malformed queue JSON fail without overwriting it. Use a per-path in-process `threading.RLock` plus the platform file lock so threads and processes share one mutation boundary.

- [ ] **Step 4: Add duplicate-name and imported-ID regressions**

Add CLI tests proving:

1. creating `demo` twice returns `EXIT_CONFLICT` and leaves the first session bytes unchanged;
2. a failed second create cannot remove the first session or its `.omp-path`;
3. importing an archive with `--conflict rename` fails with `EXIT_VALIDATION` when its `omp_session_id` already belongs to another local session.

Use temporary state and `--no-install`; do not invoke a service manager.

- [ ] **Step 5: Run identity tests and verify RED**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_cli.py tests/test_release_features.py -k 'same_name or duplicate_name or imported_omp_id'
```

Expected: existing state is overwritten/deleted or duplicate imported OMP ID is accepted.

- [ ] **Step 6: Implement exclusive session creation**

Guard session creation with a store-level lock, check name existence and OMP-ID uniqueness while locked, then atomically create the session. Translate `FileExistsError` into `CliError(code="conflict", exit_code=EXIT_CONFLICT)`. Use exclusive creation for create/adopt and rename-import; for replace-import, exclude the replaced name while enforcing uniqueness against every other session. Rollback must delete only state created by the current operation and restore backed-up files for replace.

- [ ] **Step 7: Verify Task 3 GREEN**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_core.py tests/test_cli.py tests/test_release_features.py
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit Task 3**

```bash
git add plugins/hermes-omp/src/hermes_omp/core.py plugins/hermes-omp/src/hermes_omp/cli.py plugins/hermes-omp/tests/test_core.py plugins/hermes-omp/tests/test_cli.py plugins/hermes-omp/tests/test_release_features.py
git commit -m "fix: serialize queues and reserve session identity"
```

---

### Task 4: Public argparse builder without adapter round-trip

**Files:**
- Modify: `plugins/hermes-omp/src/hermes_omp/cli.py`
- Modify: `plugins/hermes-omp/plugin/cli.py`
- Modify: `plugins/hermes-omp/tests/test_plugin.py`
- Modify: `plugins/hermes-omp/tests/test_cli.py`

**Interfaces:**
- Produces: `configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser`.
- Produces: `dispatch_namespace(args: argparse.Namespace, paths: Paths | None = None) -> int`.
- Preserves: `build_parser()`, `main(argv)`, command names, arguments, and exit codes.

- [ ] **Step 1: Add failing Hermes-adapter zero-value tests**

Build the parser through the actual plugin `setup_fn`, parse `logs demo --lines 0 --poll-interval 0 --max-polls 0 --json`, and pass the namespace to the actual plugin handler while intercepting only the package-level namespace dispatch. Assert the received namespace contains all three explicit zero values. Add an assertion that plugin code does not access private argparse classes by exercising registration with a parser whose public methods work but which has no copied template `_actions` path. Add an adapter-level expected-failure test (for example duplicate create) and assert it returns `EXIT_CONFLICT` with the same JSON error envelope instead of raising `CliError`.

- [ ] **Step 2: Run adapter tests and verify RED**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_plugin.py -k 'zero or public_parser'
```

Expected: zero-valued options disappear during argv reconstruction or registration requires private parser actions.

- [ ] **Step 3: Extract shared parser population and namespace dispatch**

Move the existing subparser construction into `configure_parser(parser)`. `build_parser()` creates the standalone parser and calls it. Split raw `_dispatch` invocation from a shared `dispatch_namespace` error boundary: the latter discovers/ensures paths, catches `CliError`, `ValueError`, and `FileNotFoundError`, emits the existing JSON/text envelope based on `args.json`, and returns the preserved exit code. `main` parses argv and delegates to that same boundary.

In `plugin/cli.py`, import these two public package functions, call `configure_parser` on the Hermes-supplied parser, and dispatch the namespace through the shared exception boundary. Keep the current source-tree fallback only when the installed `hermes_omp` import is unavailable. Remove `_actions`, `_SubParsersAction`, positional maps, and argv reconstruction.

- [ ] **Step 4: Verify Task 4 GREEN**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_plugin.py tests/test_cli.py
```

Expected: all selected tests pass and `hermes-omp --help` still lists every command.

- [ ] **Step 5: Commit Task 4**

```bash
git add plugins/hermes-omp/src/hermes_omp/cli.py plugins/hermes-omp/plugin/cli.py plugins/hermes-omp/tests/test_plugin.py plugins/hermes-omp/tests/test_cli.py
git commit -m "refactor: share the public Hermes CLI parser"
```

---

### Task 5: Truthful cross-platform service definitions

**Files:**
- Modify: `plugins/hermes-omp/src/hermes_omp/cli.py`
- Modify: `plugins/hermes-omp/src/hermes_omp/runtime.py`
- Modify: `plugins/hermes-omp/src/hermes_omp/service.py`
- Modify: `plugins/hermes-omp/tests/test_bridge_service.py`
- Modify: `plugins/hermes-omp/tests/test_cli.py`

**Interfaces:**
- Produces: internal service command `python -m hermes_omp.runtime NAME --root STATE_ROOT`.
- Preserves: public `hermes omp run NAME` and backend class method signatures.
- Produces: systemd enable/disable runner calls and launchd bootout/bootstrap semantics through injected runners only.

- [ ] **Step 1: Add failing profile-root and backend command tests**

Assert generated commands contain `--root` followed by the exact temporary `Paths.root`. Assert runtime `main(["demo", "--root", root])` constructs `Paths(Path(root))` through an injected `run` call.

For injected runner call logs, assert:

- launchd stop uses `launchctl bootout`, not `launchctl kill`;
- launchd start bootstraps the plist before kickstart;
- systemd install with `activate=True` calls `daemon-reload` then `enable hermes-omp-demo.service`;
- systemd remove calls `disable --now` before deleting/reloading;
- Windows arguments equal `subprocess.list2cmdline(command[1:])` after XML escaping;
- Windows XML declares UTF-8, matching `atomic_write` text encoding;
- systemd `WorkingDirectory` and `ExecStart` preserve spaces and escape `%` as `%%`.

- [ ] **Step 2: Run service tests and verify RED**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_bridge_service.py tests/test_cli.py -k 'service or launchd or systemd or windows or root'
```

Expected: current kill semantics, missing enable calls, POSIX Windows quoting, and absent explicit state root fail assertions.

- [ ] **Step 3: Implement explicit root and native manager semantics**

Pass `paths.root` in every generated runtime command and parse `--root` only in the internal runtime module. Update launchd/systemd/Windows definitions and runner calls to match the literal assertions. Reject newline-bearing systemd paths rather than emitting ambiguous units. Keep all commands as argv arrays.

- [ ] **Step 4: Verify Task 5 GREEN**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_bridge_service.py tests/test_cli.py tests/test_runtime.py
```

Expected: all selected tests pass without touching a real service manager.

- [ ] **Step 5: Commit Task 5**

```bash
git add plugins/hermes-omp/src/hermes_omp/cli.py plugins/hermes-omp/src/hermes_omp/runtime.py plugins/hermes-omp/src/hermes_omp/service.py plugins/hermes-omp/tests/test_bridge_service.py plugins/hermes-omp/tests/test_cli.py
git commit -m "fix: make service lifecycle definitions durable"
```

---

### Task 6: Clean manifest and distribution artifacts

**Files:**
- Modify: `plugins/hermes-omp/plugin/plugin.yaml`
- Modify: `plugins/hermes-omp/pyproject.toml`
- Modify: `plugins/hermes-omp/tests/test_plugin.py`
- Modify: `plugins/hermes-omp/tests/test_distribution.py`
- Modify: `plugins/hermes-omp/dist/*` only after tests prove the package fix

**Interfaces:**
- Preserves: plugin id `omp`, version `0.2.0rc1`, manual directory discovery, and console script `hermes-omp`.
- Produces: sdist containing `src`, `plugin`, `skills`, `docs`, `tests`, `examples`, and root package metadata but no virtualenv/cache/build/dist/artifact tree.

- [ ] **Step 1: Add a failing fresh-sdist boundary test**

Use `subprocess.run([sys.executable, "-m", "build", "--sdist", "--outdir", tmp_path], check=True, cwd=root)` and inspect the generated tar. Assert required suffixes exist and reject members containing any path component matching `.venv*`, `__pycache__`, `.pytest_cache`, `dist`, `build`, or `artifacts`.

Remove the existing early return when no checked-in archive exists; the test must always build the artifact it inspects.

- [ ] **Step 2: Run distribution test and verify RED**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_distribution.py -k source_archive
```

Expected: the fresh sdist includes `.venv-verify` from the plugin root.

- [ ] **Step 3: Add Hatchling exclusions**

Keep the explicit sdist `include` list and add Git-style root exclusions, which override includes:

```toml
exclude = [
  "/.venv*",
  "/**/.venv*",
  "/**/__pycache__",
  "/.pytest_cache",
  "/build",
  "/dist",
  "/artifacts",
]
```

Remove the ignored `provides.cli_commands` block from `plugin.yaml`. Update plugin tests to validate observable registration only; do not replace it with another source-text assertion or a false `provides_tools` declaration.

- [ ] **Step 4: Verify Task 6 GREEN**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_distribution.py tests/test_plugin.py
```

Expected: the freshly built sdist has the intended allowlisted surface and plugin registration tests pass.

- [ ] **Step 5: Build canonical worktree artifacts**

Remove only the worktree’s two current `0.2.0rc1` distribution files, then run:

```bash
.venv-verify/bin/python -m build
```

Recreate `dist/SHA256SUMS` from the new wheel and sdist in sorted filename order and verify it with `shasum -a 256 -c dist/SHA256SUMS`.

- [ ] **Step 6: Commit Task 6**

```bash
git add plugins/hermes-omp/plugin/plugin.yaml plugins/hermes-omp/pyproject.toml plugins/hermes-omp/tests/test_plugin.py plugins/hermes-omp/tests/test_distribution.py plugins/hermes-omp/dist
git commit -m "fix: exclude local environments from distributions"
```

---

### Task 7: CI hardening and truthful operator documentation

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `plugins/hermes-omp/.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `RELEASING.md`
- Modify: `plugins.json`
- Modify: `plugins/hermes-omp/README.md`
- Modify: `plugins/hermes-omp/docs/INSTALL.md`
- Modify: `plugins/hermes-omp/docs/COMPATIBILITY.md`
- Modify: `plugins/hermes-omp/docs/CONFIGURATION.md`
- Modify: `plugins/hermes-omp/docs/CLI.md`
- Modify: `plugins/hermes-omp/docs/RECOVERY.md`
- Modify: `plugins/hermes-omp/docs/MIGRATION.md`
- Modify: `plugins/hermes-omp/docs/PUBLISHING.md`
- Modify: `plugins/hermes-omp/docs/SECURITY.md`
- Modify: `plugins/hermes-omp/docs/TROUBLESHOOTING.md`
- Modify: `plugins/hermes-omp/CHANGELOG.md`

**Interfaces:**
- Preserves: CI’s 3 OS × 3 Python matrix and current two-artifact installation model.
- Uses: `actions/checkout` commit `11d5960a326750d5838078e36cf38b85af677262` (`v4`).
- Uses: `actions/setup-python` commit `a26af69be951a213d495a4c3e4e4022e16d87065` (`v5`).

- [ ] **Step 1: Harden existing workflows without adding acquisition or cost**

Set `permissions: contents: read`. Add workflow/ref concurrency cancellation and bounded job `timeout-minutes`. Replace mutable action tags with the exact full SHAs above and retain comments `# v4` / `# v5`. Do not reduce matrix entries, add caches, install Hermes, upload artifacts, sign, attest, or publish. Add a comment to the nested workflow stating it is used only when that plugin directory is a standalone repository.

- [ ] **Step 2: Validate workflow syntax locally**

Run Ruby’s YAML parser over both files after replacing `${{ ... }}` expressions with inert strings in-memory, not on disk. Expected: both documents parse; no workflow executes.

- [ ] **Step 3: Correct installation and identity documentation**

Document the current RC lifecycle precisely:

1. install the wheel into the same isolated Python environment that supplies Hermes;
2. copy `plugin/` to the active profile’s plugin root as `omp`;
3. run `hermes plugins doctor <source-or-copied-plugin-path> --ci` before enabling trusted code;
4. enable `omp`;
5. run operational `hermes omp doctor --json`;
6. disable/remove plugin id `omp`, then uninstall distribution `hermes-omp` during uninstall.

State that `hermes plugins install ./plugin` is unsupported in Hermes 0.21.0. Qualify `~/.hermes` as the default profile only. Separate session `update` from package/plugin upgrade. Do not invent a managed upgrade command for the manual-copy installation.

- [ ] **Step 4: Correct compatibility, security, and recovery claims**

Record Hermes 0.21.0 / tag `v2026.8.31` / commit `29112bef099274229cadff79cdff7bf7b99c4b77` as the verified baseline while leaving any wider minimum-support policy explicitly unverified. State Hermes itself runs on Python `>=3.11,<3.14`; standalone package unit coverage may include older Python separately.

State that executable override environment variables and full environment inheritance are production behavior, not security boundaries. State that regex redaction and PID liveness are heuristics. Describe fake-process E2E honestly. Make recovery commands exact where existing CLI supports them; do not promise complete archive confidentiality, process identity, or native Linux/Windows host validation.

- [ ] **Step 5: Correct root DX and evidence language**

Replace the false “relative scripts work from any current directory” claim with root-relative versus absolute invocation guidance. Distinguish fresh command output from historical `artifacts/` transcripts. Remove the machine-local `source_repository` value from `plugins.json` while retaining `source_commit` as historical import provenance; do not invent a public URL. Update the catalog test accordingly.

- [ ] **Step 6: Run documentation consistency checks**

Search the repository for the rejected forms `hermes plugins install ./plugin`, `plugins uninstall hermes_omp`, claims that nested `provides.cli_commands` is recognized, and unqualified “all 16 E2E” language. Expected: no active documentation makes those claims; historical logs may still contain old output and must be labeled historical.

- [ ] **Step 7: Run affected tests**

Run:

```bash
plugins/hermes-omp/.venv-verify/bin/python -m pytest -q tests plugins/hermes-omp/tests/test_distribution.py plugins/hermes-omp/tests/test_plugin.py
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit Task 7**

```bash
git add .github/workflows/ci.yml README.md CONTRIBUTING.md RELEASING.md plugins.json tests/test_monorepo.py plugins/hermes-omp/.github/workflows/ci.yml plugins/hermes-omp/README.md plugins/hermes-omp/docs plugins/hermes-omp/CHANGELOG.md
git commit -m "docs: align hermes-omp operations with Hermes 0.21"
```

---

### Task 8: Exact-runtime and release validation evidence

**Files:**
- Modify: `plugins/hermes-omp/dist/*` only if the final canonical build differs from Task 6
- No real profile, service, gateway, or session files

**Interfaces:**
- Consumes: exact Hermes source at commit `29112bef099274229cadff79cdff7bf7b99c4b77`.
- Produces: command output for final report, not a claim based on historical transcripts.

- [ ] **Step 1: Run focused and complete test suites**

Run the focused test files changed by Tasks 1–7, then:

```bash
.venv-verify/bin/python -m pytest -q
```

from `plugins/hermes-omp`, and:

```bash
plugins/hermes-omp/.venv-verify/bin/python -m pytest -q tests
```

from the monorepo root. Record exact counts and durations.

- [ ] **Step 2: Run isolated subprocess E2E alone**

Run:

```bash
.venv-verify/bin/python -m pytest -q tests/test_e2e.py
```

Confirm teardown leaves no child owned by the test. Do not inspect unrelated processes.

- [ ] **Step 3: Install-test wheel and sdist independently**

Create two temporary Python 3.13 virtual environments under the worktree, install one canonical wheel into one and one canonical sdist into the other, and run each environment’s actual `hermes-omp --help` plus `hermes-omp doctor --json` with temporary `HERMES_HOME` and explicit harmless fake binary overrides. Do not modify the workstation Hermes installation.

- [ ] **Step 4: Validate against exact Hermes 0.21.0**

Create a separate Python 3.13 venv under the worktree. Install Hermes from the already verified source checkout at commit `29112bef099274229cadff79cdff7bf7b99c4b77` and install the canonical hermes-omp wheel into that same venv. Use a fresh temporary `HERMES_HOME`, copy the worktree’s native plugin directory to `<temp-home>/plugins/omp`, enable only that temporary plugin, and run:

```bash
hermes plugins doctor <worktree>/plugins/hermes-omp/plugin --ci
hermes omp --help
```

Capture versions first. Do not start Hermes chat, a gateway, OMP, or a service.

- [ ] **Step 5: Verify reproducible builds and checksums**

Build twice into two empty temporary output directories with the same `SOURCE_DATE_EPOCH` equal to the current source commit timestamp. Compute sorted SHA-256 manifests and compare wheel-to-wheel and sdist-to-sdist. If bytes differ, report the exact differing artifact without claiming reproducibility. Build the final canonical `dist` once under the same epoch, refresh `dist/SHA256SUMS`, and run:

```bash
shasum -a 256 -c dist/SHA256SUMS
```

- [ ] **Step 6: Run plugin doctor through repository command path**

Prepend the exact-Hermes venv to `PATH` and run only:

```bash
./scripts/plugins doctor hermes-omp
```

with a fresh temporary `HERMES_HOME`. Do not run `scripts/plugins all` if it would rebuild with a different environment or touch a non-temporary profile.

- [ ] **Step 7: Verify final Git state**

Run `git status --short --branch`, `git diff --check`, `git log --oneline --decorate` for this branch, and a diff stat from the branch base. Confirm no ignored venv/cache/build directory entered version control.

- [ ] **Step 8: Commit final canonical artifacts if changed**

If Task 8 changed tracked distribution bytes or `dist/SHA256SUMS`:

```bash
git add plugins/hermes-omp/dist
git commit -m "chore: refresh verified hermes-omp artifacts"
```

Then repeat checksum verification and Git-state reporting against the committed tree.