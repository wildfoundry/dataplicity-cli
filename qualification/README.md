# Cross-repository transport qualification

Run `test_router_integration.py` explicitly with both the CLI and router source
trees and their dependencies importable. It needs permission to bind loopback
TCP ports, uses `127.0.0.1` and `127.0.0.2`, and requires `openssl` to generate a
temporary TLS certificate. The test retains certificate verification.

```sh
PYTHONPATH=/path/to/dataplicity-router-prelude python -m pytest -q qualification/test_router_integration.py
```

The test starts the actual router HTTP and M2M WebSocket handlers on two hosts.
Actual CLI sessions publish and consume a local TCP service through the second
host's ingress. It checks six concurrent HTTP streams, a large request with the
server's 1 KiB frame limit, TCP half-close, graceful session cleanup, and router
revocation against a client that deliberately ignores channel-close messages.
An authorised connection continues working after the adversarial connection is
revoked.

The central authentication and registry API and Redis peer directory are test
adapters. Router challenge verification, channel admission, forwarding,
renewal, revocation, host routing and TCP bridging execute their implementation
code. This is transport contract evidence; it does not qualify Django
permissions, production Redis discovery, signed native binaries, production
load or deployment to staging. Those require their own release gates.

On 2026-10-10, this qualification passed both tests in 0.83 seconds on Linux,
with the actual CLI and router sources and network-enabled loopback sockets.
The test exercised six simultaneous streams, including a 128 KiB request,
half-close, server revocation despite ignored close notifications, and continued
authorised traffic. The frozen Linux executable also passed the existing real
HTTP port-forward smoke test. These results retain the scope limits above.
