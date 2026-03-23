import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    dist_dir = Path(__file__).resolve().parents[1] / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for file in sorted(dist_dir.glob("*")):
        if not file.is_file():
            continue
        if file.name == "SHA256SUMS.txt":
            continue
        lines.append(f"{sha256_file(file)}  {file.name}")

    (dist_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


if __name__ == "__main__":
    main()
