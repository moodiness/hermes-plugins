# Threat model and security audit

Trust boundaries are OMP output, project files, inbound user messages, and service-manager state. All are untrusted. Controls: argv lists without shell interpolation; strict slugs; atomic private files/directories; route/sender/question correlation; replay event IDs; question expiry; risky-action denylist for automatic answers; secret redaction; durable FIFO delivery; no credentials, Telegram API, state.db, gateway imports, or command execution from message text.

Sensitive external actions (push/publish/review/comment/merge/deploy, permissions/secrets, payments, destructive or privileged commands) always require explicit authorized input. RC1 cannot invoke a Hermes approval API because no documented public standalone approval API was found; the classifier defaults closed.

Residual risks: an OS account compromise can read runtime state; at-least-once delivery can duplicate the final event after an acknowledgement crash; regex redaction cannot recognize every novel secret format; the file inbound adapter relies on filesystem ownership. Keep `$HERMES_HOME/omp` mode 0700 and state 0600.
