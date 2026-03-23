import hashlib
import argparse
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", default="dist", help="Directory containing release artifacts")
    parser.add_argument("--output", default="SHA256SUMS.txt", help="Output filename (written inside dist-dir)")
    parser.add_argument("--file", action="append", dest="files", default=None, help="Filename to include (repeatable)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    dist_dir = (repo_root / args.dist_dir).resolve()
    dist_dir.mkdir(parents=True, exist_ok=True)

    requested = set(args.files or [])
    lines = []
    for file in sorted(dist_dir.glob("*")):
        if not file.is_file():
            continue
        if file.name == args.output:
            continue
        if requested and file.name not in requested:
            continue
        lines.append(f"{sha256_file(file)}  {file.name}")

    (dist_dir / args.output).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


if __name__ == "__main__":
    main()
