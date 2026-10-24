# Security model

hermes-finance keeps your money data on your computer, and refuses to start where it would be exposed without
protection.

## What stays on your computer

- **The database is a local file**: `data/finance.db`, SQLite in WAL mode. The directory is 0700 and the database
  files are 0600. Every file the CLI creates is owner-only (`umask 077`).
- **Bank access tokens** are stored in the database and appended to `data/tokens.backup.jsonl` (0600, fsync'd). No
  page or API response includes one. Through `finance query` and every other read-only connection they read as
  `NULL`, as do the login's hashes, the 2FA key, session and CSRF tokens, and MCP token hashes.
- **Keys you paste** (Plaid, the AI key) go to the secrets file, `~/.config/hermes-finance/secrets.env`, 0600 in a
  0700 directory. They are never sent back to the browser. Alpaca's read-only key has its own 0600 file.
- **Nothing secret is printed or logged.** Output and logs pass through a redactor that removes the Plaid secret,
  every loaded access token, and anything shaped like a Plaid access or public token. Plaid request and response
  bodies are never logged.
- **Download backup** (Settings → Data) is a consistent copy of the database with every credential taken out: bank
  tokens, the login, sessions, lockouts and MCP tokens. The file is vacuumed so nothing is left in its free pages. It
  never includes the secrets file. After restoring from it, you reconnect each bank and set the login up again.

## Access modes {#access-modes}

Who may open the dashboard is one setting with three modes: **Settings → Access & security**, or `finance access`.

| Mode | `finance access mode` | Who gets in |
|---|---|---|
| **This computer only** (a new install's default) | `local` | Requests to a localhost address that didn't come through a proxy. No login. |
| **Behind your proxy login** | `proxy` | A proxy on this computer signs you in (Tailscale Serve, Cloudflare Access, Authelia, oauth2-proxy) and forwards the request with your identity in a trusted header (`Tailscale-User-Login` by default; the header name is a setting). The identity must be on your allowed list. |
| **Built-in login** | `builtin` | Every request needs a session from the app's own login. `/mcp` needs a bearer token. |

### This computer only

The server binds `127.0.0.1` and answers only a localhost `Host` (`127.0.0.1`, `localhost` or `[::1]` with the port).
A request carrying any proxy header (`X-Forwarded-*`, `Forwarded`, `Tailscale-User-*`) gets 403, and the log says why.

### Behind your proxy login

For example, with Tailscale Serve on the same machine:

```sh
tailscale serve --bg --https=8765 http://127.0.0.1:8765
uv run finance serve --public-url https://<machine>.<tailnet>.ts.net:8765 --allow-user you@example.com
```

- The app still binds loopback only. It refuses a non-loopback `--host` in this mode, because anyone reaching the port
  directly could send the identity header.
- A request for a public URL must carry an allowed identity. A localhost request works as in *This computer only*.
  Proxy headers never grant access on their own.
- The first start with `--public-url` saves the URL and the `--allow-user` logins (or `FINANCE_PUBLIC_URLS` /
  `FINANCE_ALLOWED_USERS`, comma-separated). Manage them later in Settings, or with `finance access allow LOGIN` /
  `finance access disallow LOGIN`. The page and the CLI show identities masked.

### Built-in login

For hosting where a proxy doesn't sign people in. You set it up on the Settings page from this computer.

- One account: a username and a password (12 to 256 characters, hashed with argon2id), plus a required authenticator
  app (TOTP, RFC 6238: 6 digits, 30-second steps, a code accepted once), and 8 one-time recovery codes.
- The session is a random 256-bit token in an `HttpOnly; SameSite=Strict` cookie, `Secure` unless it's plain HTTP on
  localhost. Only its SHA-256 is stored. It expires after 8 hours idle and 7 days at most.
- Every state-changing request (POST, DELETE) must send the session's CSRF token.
- 5 wrong tries in a row lock that username, and that address, for 15 minutes.
- Locked out? `finance access reset-login` on the machine removes the login (password, authenticator, recovery codes,
  sessions) and goes back to *This computer only*.

## Fail-closed start

`finance serve` refuses to start (exit 2, with the reason and what to do) when the app would be reachable from other
devices and nothing protects it:

- a non-loopback `--host` in *This computer only*, or in *Behind your proxy login*;
- public URLs (`--public-url` / `FINANCE_PUBLIC_URLS`) in *This computer only*;
- *Behind your proxy login* with no allowed identity;
- *Built-in login* chosen but not set up, with a non-loopback `--host` or public URLs.

For example, on a fresh install:

```text
$ finance serve --host 0.0.0.0
error: refusing to listen on 0.0.0.0: Access is “This computer only”, so the app only binds a loopback address (127.0.0.1). To open it from other devices, choose “Behind your proxy login” (e.g. tailscale serve) or “Built-in login” in Settings → Access & security first:
  tailscale serve --bg --https=8765 http://127.0.0.1:8765
  finance serve --public-url https://<machine>.<tailnet>.ts.net:8765 --allow-user <your login>
```

On a fresh install, a start refused on a non-loopback `--host` creates nothing, not even the database.

## Every request

- **Host and Origin checks** block DNS rebinding and cross-site requests. The `Host` must be localhost or one of your
  public URLs. Any `Origin` must be one of the app's own. Writes must be `application/json`.
- **Headers:** every response has a Content-Security-Policy (scripts only from the app itself and `cdn.plaid.com`, for
  Link), `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` and `frame-ancestors 'none'`. The page has
  no inline scripts.
- **Errors** never carry a traceback to the browser.

## Agents and writes

- **MCP is read-only by default.** Write tools are listed only after `finance mcp allow-writes on`. Even then, a call
  is only saved as a pending action. Nothing changes until you approve it in Settings → Connect other agents. See
  [AI modes](ai-modes.md#mcp-server).
- **The Hermes plugin** asks for your approval before each write, and blocks writes in scheduled jobs and on
  unattended platforms.
- **The Ask panel** shows a write as an approval card. Nothing is applied until you tap Approve.
- **Deleting a categorization rule** has no agent tool: only you can do it, in Settings or with `finance rules delete`.

## What leaves your computer

| Destination | When | Notes |
|---|---|---|
| Plaid (`production.plaid.com` or `sandbox.plaid.com`) | Link, sync, reconnect, refresh, bank logos | Your keys and the access tokens, as Plaid's API needs |
| `cdn.plaid.com` (from your browser) | Opening Plaid Link | The only third-party script the page loads |
| A bank's own website | A bank with no logo at Plaid | Its icon, fetched once over HTTPS by the server |
| `financialmodelingprep.com` | Holdings logos (stocks, ETFs, funds) | The ticker only |
| NHTSA (`vpic.nhtsa.dot.gov`) | You enter a vehicle's VIN | The VIN, decoded once |
| Carfax (through your local Chrome), Redfin | A vehicle's or home's estimate; both off on a new install | VIN, mileage, ZIP code and condition; the home's address |
| Wikimedia Commons, OpenStreetMap | Pictures for a vehicle or home (on by default) | Year, make and model; the home's map location |
| Alpaca (`api.alpaca.markets`) | Only with an Alpaca key | Read-only GETs; a paper-trading key is refused |
| Your AI provider | API-key mode | Your question and the read results ([details](ai-modes.md#api-key-the-app-runs-the-loop)) |
| Hermes's model provider | Hermes mode | The same, through Hermes ([details](ask-hermes-bridge.md#privacy)) |

Every valuation and picture call is counted against a monthly cap before it is sent. See
[Settings keys](reference/settings-keys.md#valuation-sources).

## Reporting a problem

This is a personal project with best-effort support. If you find a security issue, report it privately to the
maintainer instead of opening a public issue.
