# Publishing plan

RC preparation is local only. Fresh release evidence must cover the supported OS/Python package matrix, fake-process subprocess E2E, both distribution artifacts, archive contents/checksums, and exact Hermes 0.21.0 plugin discovery in an isolated temporary profile. Native Linux/Windows manager behavior, real services, gateway traffic, restarts, and reboots are not part of that evidence.

The manual consumer lifecycle remains two-part: install the reviewed `hermes-omp` wheel into the environment that supplies Hermes, and copy the reviewed native `plugin/` directory as plugin id `omp`. The sdist is a source artifact, not a replacement for either installation step. `hermes plugins install ./plugin` is unsupported in Hermes 0.21.0.

HMAC-authenticated session archives are an operator feature, not release-artifact signing. Remote repository creation, release signing/attestation, uploads, tag pushes, catalog submission, and announcements remain deferred until explicit owner approval; no such action is part of this release-candidate preparation.

The canonical build is `python -m pip install -r requirements-build.txt && sh scripts/build-release.sh`. The script derives `SOURCE_DATE_EPOCH` from the newest commit touching the plugin unless a deterministic value is supplied, cleans `build/` and `dist/`, builds without isolation using pinned tools, and writes `dist/SHA256SUMS`. Two clean invocations from the same checkout must be byte-identical.
