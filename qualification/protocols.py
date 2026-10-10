"""Real SSH server/client qualification shared by local and live acceptance."""
import socketserver
import threading


class SSHFixture:
    """A real SSH server with fixed test credentials and a single inert command."""
    def __init__(self):
        import paramiko
        self.key = paramiko.RSAKey.generate(2048)
        key = self.key

        class Authority(paramiko.ServerInterface):
            def __init__(self):
                self.executed = threading.Event()

            def check_auth_password(self, username, password):
                return paramiko.AUTH_SUCCESSFUL if (username, password) == ("qualification", "test-only") else paramiko.AUTH_FAILED

            def get_allowed_auths(self, username):
                return "password"

            def check_channel_request(self, kind, channel_id):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_exec_request(self, channel, command):
                if command != b"qualification":
                    return False
                self.executed.set()
                return True

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                with paramiko.Transport(self.request) as transport:
                    transport.add_server_key(key)
                    authority = Authority()
                    transport.start_server(server=authority)
                    channel = transport.accept(5)
                    if channel is not None:
                        if authority.executed.wait(5):
                            channel.sendall(b"named-tunnel-ssh-ok\n")
                            channel.send_exit_status(0)
                            channel.shutdown_write()
                            transport.join(timeout=5)
                        channel.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def probe(self, port):
        import paramiko
        with paramiko.SSHClient() as client:
            # The fixture's public key is known before connection; reject changes.
            client.get_host_keys().add(f"[127.0.0.1]:{port}", self.key.get_name(), self.key)
            client.connect("127.0.0.1", port=port, username="qualification", password="test-only",
                           allow_agent=False, look_for_keys=False, timeout=5, auth_timeout=5, banner_timeout=5)
            _, stdout, stderr = client.exec_command("qualification", timeout=5)
            assert stdout.read() == b"named-tunnel-ssh-ok\n"
            assert stderr.read() == b""
            assert stdout.channel.recv_exit_status() == 0

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

