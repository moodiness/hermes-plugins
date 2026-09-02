# Security tooling decisions

## OpenSSF Scorecard

A Scorecard workflow is intentionally not enabled while this is a private repository. Scorecard's recommended publishing flow uploads results to the public OpenSSF REST API and uses OIDC and security-event permissions. Publishing private-repository metadata is not appropriate here, and a local-only run would add cost while omitting the ecosystem signal the workflow is designed to provide.

Reconsider Scorecard if the repository becomes public. Until then, CodeQL, dependency review, Dependabot, pinned actions, least-privilege permissions, policy tests, and branch protection provide the repository controls.
