# CLI credential recipients

The user API client is bound to the server and optional path prefix selected at
construction. Relative requests and absolute URLs within that recipient remain
supported. Scheme, canonical host, effective port and path boundary must match;
userinfo, fragments, path traversal, sibling prefixes and remote HTTP are
rejected before renewal or HTTP dispatch. Explicit loopback HTTP remains
available for development. Requests do not follow redirects with credentials.

Changing the configured recipient requires new authentication. A command-line
override clears credentials in its selected in-memory configuration; changing
the saved server persists that clearing. Equivalent host case/default HTTPS port
does not discard the session. Token reload cannot import credentials from a
different saved recipient. An existing API client cannot silently change server
when its shared configuration object is changed.

Tunnel publishers construct an independent client for the selected Runtime
recipient. That client has no saved user JWT, organisation key, cookies or token
persistence/reload callbacks. Its scoped publisher proof cannot refresh or clear
the saved human account. Background renewal follows the client running the
command. CLI startup and local configuration/auth-status operations perform no
automatic user renewal; user API requests retain proactive renewal and existing
long-running user commands retain keepalive. Primary password/MFA authentication
uses an anonymous client, as the native code flow already does. Installing user
JWTs removes an old organisation key, and installing a key removes old user JWTs.

An explicit no-op requests authenticator prevents netrc or Session Basic auth
from replacing the chosen API proof. Environment proxy and CA support remain
enabled. Transport exceptions return a safe message because requests can include
credential headers or private URLs in exception text. Structured
`session_expired` is an authentication failure only at the existing authentication
status codes; ordinary permission failures do not acquire refresh authority.

## Session ownership and persistence

Each saved session has a local opaque identity. Explicit sign-in, key installation
and sign-out replace that identity, even when the credential bytes are unchanged.
Legitimate renewal retains it. A client captures its owner at construction;
admission, renewal, retry, persistence and response publication require that same
owner. An old request cannot pick up a replacement account, replay its mutation
under that account, clear it after a delayed denial, or return old account data
after replacement. Primary sign-in constructs a new client after committing its
new session. Local identity is concurrency metadata, never server authority.

Configuration transactions use an OS process lock and replace a complete,
synced private file. They release locks before HTTP. Writes compare the observed
session against the latest file, preserve concurrent legitimate renewal and
independent preference edits, and refuse conflicting changes. A persistent lock
inode prevents waiters locking different files; contention has a bounded wait.
Legacy files acquire an identity from the exact unchanged file rather than from
unverified JWT claims. A changed legacy file requires restarting the command.
Unknown configuration metadata is preserved.

Before renewal, the client persists one recovery UUID bound to that session and
the refresh input digest. Competing processes and a process restarted after a
lost response reuse it. Only the current owner and expected refresh input can
commit a result or revoke credentials. A delayed older reply cannot roll back a
newer rotation or erase its recovery receipt. Transport failure preserves the
input and receipt; the server's replay policy decides whether recovery remains
valid. Malformed credentials are refused before installation or transmission.

## Validation

Run the unit suite with its configured coverage gate, the qualification contract checks, and the platform build and installation checks. Tests use synthetic accounts, temporary configuration and loopback services. Do not use personal credentials or production accounts for tests.

See the README for supported sign-in and sign-out behaviour and backend compatibility requirements.
