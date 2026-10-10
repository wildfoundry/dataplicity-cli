# Private development tunnels

Development tunnels connect a local TCP service privately between Dataplicity
CLI instances in a paying organisation. Install the ordinary Dataplicity CLI
on Windows, macOS or Linux and sign in with `dataplicity auth login`.

On the computer running the service:

```sh
dataplicity tunnel publish test-api --port 3000
```

On an authorised developer's computer:

```sh
dataplicity tunnel ls --json
dataplicity tunnel connect test-api --local-port 8080
curl http://localhost:8080/
```

The consumer listens only on `127.0.0.1`. Its local port can differ from the
publisher's target port. The publisher connects only to its own loopback TCP
service; start that service before publishing. TCP services such as HTTP,
WebSocket, SSH and databases can use the same forwarding command. Use the
existing organisation hash with `--org ORG` to select an organisation explicitly.
Users with multiple organisations must select one.

Your organisation administrator controls who may publish, replace or connect
to each name. Publishing does not grant access to consume services. An
ordinary member needs an explicit access grant. Development Tunnels under
Networking in the web application shows live services and permitted management
actions. Historical events appear separately from active services. Tunnels do
not count as managed devices.

For an unattended publisher, an authorised administrator can issue a scoped
credential for a specific name and permitted target ports:

```sh
dataplicity tunnel token create test-api --port 3000 --org ORG
```

The secret is written to standard output once. Store it in your existing secret
manager and inject it into the publisher environment:

```sh
dataplicity tunnel publish test-api --port 3000 --org ORG --token-env DATAPLICITY_TUNNEL_TOKEN
```

Alternatively pipe a secret manager's output into `--token-stdin`. Supply the
environment variable's **name**, never the token itself, as a command argument.
Publisher tokens cannot list services, connect as consumers, manage credentials
or access managed devices. Broad API keys cannot authenticate tunnel operations.
`tunnel token ls`, `tunnel token rotate CREDENTIAL_UUID` and
`tunnel token revoke CREDENTIAL_UUID` manage credentials within the selected
organisation. Rotation writes the replacement secret once and invalidates the
old credential's authority.

The currently authorised publisher owns the live name. Replacement needs
explicit replace authority (`--allow-replace` during credential issuance), ends
old TCP streams and fences the previous generation. A publisher reconnects
only its existing generation; it stops when that generation is replaced,
expired or revoked. If reconnection cannot resume, run a fresh publish command
after resolving the error. Active entries disappear on stop or presence expiry.

Ctrl+C stops publishing or closes the consumer listener and its streams.
Transport loss ends existing TCP connections; rerun `connect` to resolve and
authorise the current publisher. Name replacement, revocation and service
disablement also end affected streams. Applications must reconnect their TCP
connections. Credentials and permissions are enforced by the server.

If access is denied, ask your administrator for access to the exact service
and target port. An offline or missing service must be published again.
For a local bind error, choose an unused `--local-port`. Quota errors require
closing unused streams or reviewing the organisation's plan limits. Global
`--json` produces newline-delimited progress events for long-running commands;
`tunnel ls --json` returns paginated active inventory and accepts `--search`
and `--page`.
