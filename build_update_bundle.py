"""Create a lightweight update bundle containing only changed files.

The script uses ``git diff`` to determine which tracked files have changed
between a reference (e.g. the last release tag) and the current working tree.
Those files are copied into a temporary directory and zipped.  The resulting
archive can be distributed alongside the ``launcher.py`` so only the modified
files need to be downloaded by clients.

Example usage::

    python build_update_bundle.py --ref v1.2.3 --output dist/update.zip

Use ``--include-untracked`` when you also want to ship newly-added files that
haven't been committed yet.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable, Set


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True)


def collect_changed_files(ref: str, include_untracked: bool = False) -> Set[Path]:
    changed = {
        Path(path.strip())
        for path in _git("diff", "--name-only", "--diff-filter=ACMRT", ref, "HEAD").splitlines()
        if path.strip()
    }
    if include_untracked:
        changed.update(
            Path(path.strip())
            for path in _git("ls-files", "--others", "--exclude-standard").splitlines()
            if path.strip()
        )
    return changed


def create_bundle(files: Iterable[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        for file_path in files:
            if file_path.is_dir() or not file_path.exists():
                continue
            dest = tmp_path / file_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dest)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in tmp_path.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(tmp_path))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an incremental update archive")
    parser.add_argument(
        "--ref",
        required=True,
        help="Git reference that represents the previous release (tag, commit, etc.)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Where to write the resulting ZIP archive",
    )
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="Also include new files that are not yet tracked by git",
    )
    args = parser.parse_args()

    files = collect_changed_files(args.ref, include_untracked=args.include_untracked)
    if not files:
        print("No changes detected; nothing to bundle.")
        return

    output = Path(args.output)
    create_bundle(files, output)
    print(f"Created update bundle with {len(files)} file(s): {output}")


if __name__ == "__main__":
    main()
