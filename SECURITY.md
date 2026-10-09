# Security policy

## Supported versions

Security fixes go into the latest release and `main`.

## Reporting a vulnerability

Report privately through the repository's **Security** tab ("Report a vulnerability").
Do not open a public issue.

Include the skinflint version, your OS, what goes wrong, and how to reproduce it. Expect an
acknowledgement within a week.

## Threat model

skinflint is a local, single-user proxy.

- **It sees your credentials.** API keys and OAuth tokens pass through it on every request.
  It forwards them upstream and never logs or stores them. Request bodies are stored only
  with `store_bodies = true`.
- **Bind to loopback.** The default is `127.0.0.1`. Anyone who can reach the port can send
  requests through it with their own credentials, use up your shared budgets, and add rows
  to your ledger. skinflint warns at startup when bound to a non-loopback address. It has
  no authentication of its own.
- **The ledger is plain SQLite** in `~/.skinflint/`. It holds model names, token counts,
  costs, session ids, and hashes and sizes of request blocks. Protect it like your shell
  history.
- **Caps are enforced by the proxy, not the provider.** A client that bypasses the proxy
  (another base URL, a direct SDK call) is not capped. For a cap no client can bypass, also
  set a spend limit in the provider's console.
