"""PyInstaller hook discovery for the standalone CLI."""

from pathlib import Path


def get_hook_dirs():
    return [str(Path(__file__).resolve().parent)]
