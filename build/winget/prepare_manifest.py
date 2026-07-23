from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


def prepare_manifest(
    *,
    source_dir: Path,
    output_root: Path,
    version: str,
    installer_url: str,
    installer_sha256: str,
    release_date: str,
) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", version):
        raise ValueError("Version must be numeric, for example 0.1.7.")
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", installer_sha256):
        raise ValueError("Installer SHA-256 must contain 64 hexadecimal characters.")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
        raise ValueError("Release date must use YYYY-MM-DD.")

    source_version = None
    for manifest in source_dir.glob("*.yaml"):
        match = re.search(r"^PackageVersion:\s*(\S+)\s*$", manifest.read_text(encoding="utf-8"), re.MULTILINE)
        if match:
            source_version = match.group(1)
            break
    if source_version is None:
        raise ValueError(f"No PackageVersion found under {source_dir}.")

    output_dir = output_root / version
    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(source_dir, output_dir)

    for manifest in output_dir.glob("*.yaml"):
        text = manifest.read_text(encoding="utf-8")
        text = text.replace(source_version, version)
        text = re.sub(r"(?m)^  InstallerUrl: .+$", f"  InstallerUrl: {installer_url}", text)
        text = re.sub(
            r"(?m)^  InstallerSha256: .+$",
            f"  InstallerSha256: {installer_sha256.upper()}",
            text,
        )
        text = re.sub(r"(?m)^ReleaseDate: .+$", f"ReleaseDate: {release_date}", text)
        manifest.write_text(text, encoding="utf-8")

    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a versioned WinGet manifest from the bootstrap template.")
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--installer-url", required=True)
    parser.add_argument("--installer-sha256", required=True)
    parser.add_argument("--release-date", required=True)
    args = parser.parse_args()

    output_dir = prepare_manifest(
        source_dir=args.source_dir,
        output_root=args.output_root,
        version=args.version,
        installer_url=args.installer_url,
        installer_sha256=args.installer_sha256,
        release_date=args.release_date,
    )
    print(output_dir)


if __name__ == "__main__":
    main()
