import re
from pathlib import Path


def main() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    raw = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^\s*version\s*=\s*"([^"]+)"\s*$', raw, flags=re.MULTILINE)
    if not match:
        raise SystemExit("Unable to determine version from pyproject.toml")
    print(match.group(1))


if __name__ == "__main__":
    main()
