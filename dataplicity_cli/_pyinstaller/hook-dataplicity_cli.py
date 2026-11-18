"""Include dependencies loaded dynamically by shell detection and the SSL runtime."""

import os
from pathlib import Path
import ssl

hiddenimports = [f"shellingham.{os.name}"]
# Some Python distributions load an adjacent certificate policy file from ssl.py.
policy_file = Path(ssl.__file__).with_name("codex-rejected-cas.pem")
datas = [(str(policy_file), ".")] if policy_file.is_file() else []
