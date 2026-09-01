# Hermes OMP Safe Hardening Design

**Date:** 2026-09-01

## Goal

Bring `plugins/hermes-omp` into truthful, behavior-tested alignment with Hermes Agent 0.21.0 while preserving the current product shape: a Python runtime distribution plus a manually installed native Hermes adapter directory. Repair only defects that violate existing documented contracts. Do not choose new security policy, packaging topology, publication provenance, cost, or public-interface semantics.

## Reference baseline

- Hermes Agent 0.21.0: tag `v2026.8.31`, commit `29112bef099274229cadff79cdff7bf7b99c4b77`.
- Official examples: `NousResearch/hermes-example-plugins`, commit `38fe0fb53eff98d477f807432e965429e665ca33`.
- Current official plugin documentation retrieved 2026-09-01:
  - <https://hermes-agent.nousresearch.com/docs/developer-guide/plugins>
  - <https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins>
- Public APIs used by hermes-omp and verified in the exact Hermes source:
  - `PluginContext.register_cli_command(name, help, setup_fn, handler_fn, description="")`
  - `hermes send --to TARGET --file - --quiet`
  - profile selection through `HERMES_HOME`

The official example repository is a style and minimal-layout reference, not a production testing, packaging, CI, or durable-supervision standard.

## Current architecture

`plugins/hermes-omp` contains three distinct deliverables:

1. `src/hermes_omp`: session state, queues, CLI, bridge, OS service definitions, and OMP RPC supervisor. The wheel exposes the standalone `hermes-omp` command.
2. `plugin`: native `plugin.yaml` plus `register(ctx)`, copied into the active Hermes profile and enabled as plugin `omp`. It registers `hermes omp` and imports the separately installed Python distribution.
3. `skills/omp-service`: an operational skill stored in the source/sdist but not installed or registered by the documented wheel-plus-directory flow.

This design intentionally avoids Hermes databases, gateway internals, direct channel APIs, and credentials. It uses Hermes only through public CLI/plugin surfaces.

## Binding safety constraints

- Work only in the isolated worktree and plugin-local temporary environments.
- Never install or enable anything in a real Hermes profile.
- Never start, stop, inspect, or signal an existing OMP session or Hermes gateway.
- Never read real credentials or secrets.
- Never install a real LaunchAgent, systemd user unit, or Windows Scheduled Task on the workstation.
- Never push, publish, tag, open a PR, merge, or deploy.
- Preserve the existing two-artifact installation model for this remediation.
- Preserve public CLI names and existing state/archive schema versions unless a defect fix requires an additive internal field.
- Every production behavior change follows a demonstrated RED-GREEN cycle.

## Runtime durability design

### Inbound response commit order

An inbound answer has three states: pending, prepared, committed. Authorization and value selection prepare an RPC response without changing durable replay/question state. The supervisor writes the complete JSON line and flushes the child stream. Only after that succeeds does it atomically record the event ID as seen, clear the matching question, update activity, and move the inbox file to `processed`.

A failed write leaves both the question and the inbox event pending. A replay after a committed write is terminal. Automatic safe answers follow the same prepare/write/commit order. This restores the documented invariant: an answer is never acknowledged or consumed before OMP receives a flushed response.

### RPC stream framing

The supervisor keeps an incremental text buffer. It parses only newline-terminated JSON frames, preserves a partial suffix across reads, and preserves complete adjacent frames in order. EOF residue is logged as redacted unparsed data and never mistaken for a complete RPC event. A maximum-frame policy is deliberately deferred because no authoritative OMP limit was established.

### Child ownership and cleanup

The supervisor owns the OMP child for its entire lifetime. The owner lock remains held until the child has exited. Every exit path after spawn performs:

1. graceful child termination when still live;
2. bounded wait;
3. process-tree escalation only for the child identity created by this supervisor;
4. final wait/reap;
5. session-state update where possible;
6. owner-lock release.

No cleanup path discovers or signals unrelated processes by name. A malformed inbox item or filesystem exception cannot leave a separately-sessioned child alive after the lock disappears.

### Queue serialization

Keep the existing JSON queue format. Each mutating operation acquires an adjacent cross-process advisory lock, reloads current disk state while holding that lock, applies exactly one mutation, atomically replaces the JSON file, fsyncs as supported, then releases the lock. POSIX uses `fcntl.flock`; Windows uses `msvcrt.locking`. In-process thread serialization protects platforms whose advisory primitive is process-scoped.

This prevents a runtime acknowledgement from dropping a concurrent CLI enqueue and prevents stale writers from resurrecting delivered items. Read methods refresh from disk before reporting current state.

### Session identity

Creation/adoption reserve a session name exclusively before writing service state. Existing names fail without changing the prior session or service. OMP resume IDs are checked for uniqueness for create, adopt, and import. Import rename changes only the local session name; it cannot bypass OMP ID ownership.

Rollback removes only files created by the failed operation and never deletes pre-existing state.

## CLI adapter design

The package exposes one shared parser-population function that accepts an existing `argparse.ArgumentParser`. Both the standalone console script and native Hermes adapter use it directly. The adapter dispatches the parsed namespace through shared command logic rather than inspecting `_actions`, using `_SubParsersAction`, copying parser internals, or reconstructing argv.

Explicit zero and false-valued arguments retain their meaning. Hermes-specific code continues to import no Hermes implementation module; only the `ctx.register_cli_command` callback contract is used.

## Service definition design

Service backends retain their existing user-level scope and restart-policy vocabulary.

- launchd stop/remove uses unload/bootout semantics that suppress `KeepAlive` relaunch before reporting completion.
- systemd definitions quote/escape paths safely and install/enable consistently with the documented reboot durability contract. Tests exercise runner argv only; no real unit is installed.
- Every generated service invokes the runtime with an explicit profile-state root, avoiding reliance on service-manager-specific environment propagation. Windows XML uses Windows command-line quoting and an encoding declaration matching written bytes.
- Backend operations wait or verify only through injected runners in tests.

The approximation of Windows `restart_policy=always` remains documented and unchanged.

## Manifest and packaging design

Hermes 0.21.0 ignores nested `provides.cli_commands`; no supported CLI inventory field exists. Remove that inert block and update tests/docs to state that `register(ctx)` is the authoritative CLI declaration. Keep identity, version, author, description, and `hooks: []`.

The sdist receives explicit exclusions for all virtual environments, caches, build output, distribution output, and recorded local evidence. Distribution tests inspect a freshly built archive, reject forbidden members, and verify the intended source/plugin/docs/test files. Wheel and sdist are installed independently into fresh temporary virtual environments and exercised through their actual console entry point.

Checked-in historical transcripts are not treated as current proof. Fresh validation evidence is reported from commands run in this worktree.

## CI design

Safe CI changes do not alter the functional matrix or add network-heavy runtime acquisition:

- declare `permissions: contents: read`;
- pin existing GitHub actions to reviewed full commit SHAs, retaining version comments;
- add bounded job timeouts and concurrency cancellation;
- document that the nested workflow is active only if the plugin is extracted as a standalone repository;
- preserve all existing OS/Python matrix entries.

Installing Hermes 0.21.0 in hosted CI, reducing the matrix, caching, signing, attestations, lockfile policy, and publication automation remain decisions outside this remediation.

## Documentation design

Documentation must distinguish:

- distribution name `hermes-omp`;
- native Hermes plugin id `omp`;
- user-created session names;
- Hermes directory doctor (`hermes plugins doctor … --ci`);
- operational runtime doctor (`hermes omp doctor --json`);
- source test Python support from Hermes host-interpreter support;
- fake-based subprocess integration from exact-Hermes compatibility validation.

Correct the unsupported `hermes plugins install ./plugin` instruction, wrong `hermes_omp` uninstall id, false any-directory relative command claim, ignored manifest metadata claim, stale 0.20.6 target wording, hidden production environment overrides, and ambiguous upgrade/update terminology. The current manual wheel-plus-copy lifecycle remains canonical for this pass.

## Test design

Each regression test names one observable break and fails before its implementation:

- failed RPC write preserves pending question/event;
- successful RPC flush commits replay state;
- exceptional supervisor exit reaps its child before unlocking;
- fragmented and adjacent RPC frames preserve exact events;
- concurrent queue mutations preserve enqueue/ack state and FIFO order;
- duplicate local names and duplicate imported OMP IDs are rejected without state loss;
- Hermes adapter preserves explicit zero-valued options without private argparse internals;
- generated service definitions carry safe environment, quoting, encoding, and stop behavior;
- fresh sdist rejects `.venv*` and other forbidden members;
- subprocess integration always tears down children and is named according to what it actually verifies.

Tests use temporary `HERMES_HOME`, strict fake executables, injected service runners, and no real profile or service manager.

## Validation design

Final evidence is produced only after all targeted tests are green:

1. focused regression tests;
2. full plugin pytest suite;
3. root monorepo pytest suite;
4. isolated fake-based subprocess E2E;
5. exact Hermes 0.21.0 plugin doctor and real `hermes omp --help` discovery against a temporary profile;
6. wheel and sdist build plus clean-environment installation smoke tests;
7. archive inventory and SHA-256 verification;
8. repeated clean builds under a fixed `SOURCE_DATE_EPOCH`, reporting whether byte-for-byte reproducibility is achieved rather than assuming it;
9. final Git status and diff summary.

## Decisions deliberately deferred

The following require a separate exact question, options, and recommendation before any change:

- switching to one pip-discoverable plugin package;
- changing empty `allowed_users` from route-wide allow to deny/inherit/explicit allow-all;
- filtering or allowlisting subprocess environments;
- changing which executable/content an archive may carry;
- changing `status` exit semantics;
- changing the supported Hermes version range beyond recording 0.21.0 as the verified target;
- choosing a canonical public repository or publication provenance;
- adding Hermes acquisition to CI;
- changing dependency locking, CI matrix cost, signing, SBOM, or attestations;
- installing real user services in tests;
- automatically registering or separately distributing the repository skill.

These omissions are explicit scope boundaries, not claims that the current behavior is secure or recommended.