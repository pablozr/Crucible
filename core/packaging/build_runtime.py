"""Build the crucible-core PyInstaller onedir runtime (target OS only).

No downloads, no ``shell=True``, no cross-compilation: run on the
target OS with the project ``.venv`` active and PyInstaller already
installed. The script builds onedir from ``crucible-core.spec``,
validates ``--version``, copies the bundle into the platform package
``bin/`` dir, writes ``runtime-manifest.json`` (schema 1), and copies
the LICENSE file given explicitly via ``--license``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
CORE_DIR = PACKAGING_DIR.parent
REPO_ROOT = CORE_DIR.parent
DEFAULT_SPEC = PACKAGING_DIR / "crucible-core.spec"
VERSION_FILE = CORE_DIR / "src" / "crucible_core" / "version.py"

SCHEMA_VERSION = 1
API_VERSION = "v1"  # Contract: API v1 (see /v1/health api_version).

TARGET_BY_HOST = {
    ("win32", "amd64"): "win32-x64",
    ("win32", "x86_64"): "win32-x64",
    ("linux", "x86_64"): "linux-x64-gnu",
    ("linux", "amd64"): "linux-x64-gnu",
    ("darwin", "x86_64"): "darwin-x64",
    ("darwin", "arm64"): "darwin-arm64",
}


def derive_host_target() -> str:
    """Map the current OS/arch to a release target; fail closed."""
    machine = platform.machine().lower()
    key = (sys.platform, machine)
    target = TARGET_BY_HOST.get(key)
    if target is None:
        raise SystemExit(
            f"unsupported host for runtime build: "
            f"sys.platform={sys.platform!r} "
            f"machine={platform.machine()!r}"
        )
    return target


def executable_name(target: str) -> str:
    if target.startswith("win32"):
        return "crucible-core.exe"
    return "crucible-core"


def read_product_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if match is None:
        raise SystemExit(f"cannot parse VERSION from {VERSION_FILE}")
    return match.group(1).strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the crucible-core onedir runtime."
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=DEFAULT_SPEC,
        help="PyInstaller spec file (default: crucible-core.spec).",
    )
    parser.add_argument(
        "--output-package",
        type=Path,
        required=True,
        help="Platform package dir receiving bin/ and manifest.",
    )
    parser.add_argument(
        "--license",
        type=Path,
        required=True,
        help="Explicit LICENSE file path to copy into the package.",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="Expected target (e.g. win32-x64); must match this host.",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="Override PyInstaller dist dir (default: temp dir).",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Override PyInstaller work dir (default: temp dir).",
    )
    return parser.parse_args(argv)


def check_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit(
            "PyInstaller is not installed; install it in the target-OS "
            "project venv first (no downloads are performed here)."
        )


def run_build(spec: Path, dist_dir: Path, work_dir: Path) -> None:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        str(spec),
    ]
    # cwd anchors the spec's relative paths; list form, no shell.
    subprocess.run(cmd, cwd=PACKAGING_DIR, check=True)


def validate_bundle(bundle_dir: Path, exe_name: str, version: str) -> Path:
    exe = bundle_dir / exe_name
    if not exe.is_file():
        raise SystemExit(f"built executable not found: {exe}")
    migrations = bundle_dir / "crucible_core" / "migrations" / "env.py"
    alt_migrations = (
        bundle_dir / "_internal" / "crucible_core" / "migrations" / "env.py"
    )
    if not migrations.is_file() and not alt_migrations.is_file():
        raise SystemExit("bundled migrations not found in onedir output")
    proc = subprocess.run(
        [str(exe), "--version"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0 or proc.stdout.strip() != version:
        raise SystemExit(
            "executable --version check failed: "
            f"returncode={proc.returncode} stdout={proc.stdout!r}"
        )
    return exe


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    host_target = derive_host_target()
    target = args.target or host_target
    if target != host_target:
        raise SystemExit(
            f"target {target!r} does not match this host {host_target!r}; "
            "cross-compilation is not supported"
        )
    if not args.spec.is_file():
        raise SystemExit(f"spec file not found: {args.spec}")
    if not args.license.is_file():
        raise SystemExit(f"LICENSE file not found: {args.license}")
    check_pyinstaller()
    version = read_product_version()
    exe_name = executable_name(target)

    output_package: Path = args.output_package
    output_package.mkdir(parents=True, exist_ok=True)
    bin_dir = output_package / "bin"
    manifest_path = output_package / "runtime-manifest.json"

    with tempfile.TemporaryDirectory(prefix="crucible-dist-") as tmp:
        tmp_path = Path(tmp)
        dist_dir = args.dist_dir or (tmp_path / "dist")
        work_dir = args.work_dir or (tmp_path / "work")
        run_build(args.spec.resolve(), dist_dir, work_dir)
        bundle_dir = dist_dir / "crucible-core"
        if not bundle_dir.is_dir():
            raise SystemExit(f"onedir output not found: {bundle_dir}")
        exe = validate_bundle(bundle_dir, exe_name, version)
        digest = sha256_of(exe)

        if bin_dir.exists():
            shutil.rmtree(bin_dir)
        shutil.copytree(bundle_dir, bin_dir)

    shutil.copyfile(args.license, output_package / "LICENSE")
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "productVersion": version,
        "apiVersion": API_VERSION,
        "target": target,
        "executable": f"bin/{exe_name}",
        "sha256": digest,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"target: {target}")
    print(f"executable: bin/{exe_name}")
    print(f"sha256: {digest}")
    print(f"package: {output_package}")


if __name__ == "__main__":
    main()
