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

## Validation and limits

Tests exercise real requests URL/header preparation with synthetic responses,
foreign/sibling recipients and encoded traversal, netrc/session-auth interference,
recipient changes during admission/renewal, publisher startup/keepalive, public
primary sign-in and saved-server changes. The initial unchanged-source recipient
baseline produced 39 failures with six passing tests and six passing subtests.

Run the complete unit suite with its configured 100% coverage gate, the three
qualification contract files selected by CI, compile/help smoke checks, and a
local wheel build. Local tests redirect the default CLI configuration to a
temporary directory after coverage starts; they do not use real saved credentials.
Qualification contract tests include an actual loopback TCP bridge. Their success
is not evidence that an original installed agent passed a staging campaign.

Same-recipient account switching, conditional cross-process persistence, atomic
configuration writes, refresh response-loss recovery and actual server logout
remain separate required work. These recipient checks do not establish those
properties. Native platform build/signing, installed-client journeys and final
unchanged-agent staging checks remain held. No production or public package,
installer, Homebrew, WinGet or client-feed publication is authorised here.
