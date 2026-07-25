# Security model

## Secrets

- Secret CLI options are rejected before parsing or logging.
- Credentials live only in the user configuration directory.
- POSIX credential files require mode `0600`.
- Provider payloads, subprocess output, exceptions, frontend metadata, LaTeX
  logs, compute logs, and release reports pass through redaction.
- Source bundles, Docker contexts, SSH uploads, and release archives use
  allowlists and a final secret scan.

## Remote execution

SSH requires a non-root user, a private identity file, a pinned known-hosts
file, strict host verification, disabled agent forwarding, and disabled
forwarding. Upload paths reject credentials, environment files, private keys,
and broad directory patterns.

## Files and frontend

All workspace paths are resolved below an explicit root and reject traversal
and symlink escapes. Uploaded HTML is served as a sandboxed attachment, never as
same-origin executable content. The HTTP server binds only to loopback and
rejects untrusted Host and Origin values.

## Release

`paperforge release` recomputes every gate from authoritative artifacts. It
does not accept a caller-provided completion gate or trust a hand-written
`release-gate.json`.
