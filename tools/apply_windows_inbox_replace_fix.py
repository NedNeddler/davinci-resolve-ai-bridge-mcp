#!/usr/bin/env python3
"""Hash-guarded install of the v1.14.1 Windows inbox replace retry.

Replaces only runtime ``bridge/lifecycle.py``. Refuses any other source hash,
any other bridge version, and symlink targets. Creates a backup before writing.
Does not import the bridge, open a network listener, or read the queue token.

Stdlib only. Run from this repository or from the deployment bundle:

    python3 tools/apply_windows_inbox_replace_fix.py --runtime %USERPROFILE%\\.resolve-ai-bridge --dry-run
    python3 tools/apply_windows_inbox_replace_fix.py --runtime %USERPROFILE%\\.resolve-ai-bridge
    python3 tools/apply_windows_inbox_replace_fix.py --runtime %USERPROFILE%\\.resolve-ai-bridge --rollback
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid


ORIGINAL_SHA256 = "a5017d87e49f4e2c4bf117dcbf5ddf608f7eb293e03cf9e9436215d4ce9a61c4"
PATCHED_SHA256 = "e16a3019654267d7f0ad35140e814a89cefddf67ce0e406ae832df5d8e2dba6b"
AGENT_VERSION_LINE = 'AGENT_VERSION = "1.14.1"'
SOURCE_COMMIT = "5a8bba25cefbe8917dec10aca69828f9c02394ee"
RELATIVE_PATH = "bridge/lifecycle.py"


class ApplyError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def _candidates(*parts):
    here = Path(__file__).resolve().parent
    return (here.joinpath(*parts), here.parent.joinpath(*parts))


def payload_path():
    """Patched lifecycle.py: bundle sibling first, repository checkout second."""
    for path in (Path(__file__).resolve().parent / "bridge" / "lifecycle.py",
                 Path(__file__).resolve().parent.parent / "bridge" / "lifecycle.py"):
        if path.is_file() and not path.is_symlink():
            return path
    raise ApplyError(2, "refusing: patched bridge/lifecycle.py payload is missing")


def embedded_original_path():
    for path in _candidates("original", "lifecycle.py"):
        if path.is_file() and not path.is_symlink():
            return path
    return None


def _regular_file(path, label):
    path = Path(path)
    if path.is_symlink():
        raise ApplyError(2, "refusing: %s is a symlink" % label)
    if not path.is_file():
        raise ApplyError(2, "refusing: %s is missing" % label)
    return path


def _require_version(runtime):
    bridge = Path(runtime) / "bridge"
    if bridge.is_symlink():
        raise ApplyError(2, "refusing: bridge directory is a symlink")
    operations = _regular_file(bridge / "operations.py", "bridge/operations.py")
    lines = [line.strip() for line in operations.read_text(encoding="utf-8").splitlines()
             if line.strip().startswith("AGENT_VERSION")]
    if lines != [AGENT_VERSION_LINE]:
        raise ApplyError(
            2,
            "refusing: expected exactly one %s line in bridge/operations.py, found %r" % (AGENT_VERSION_LINE, lines),
        )
    return operations


def _publish(path, data, expected):
    """Write data to path by temp file and os.replace. Leave path unchanged on failure."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        if sha256_file(temp) != expected:
            raise ApplyError(1, "refusing to publish bytes whose hash is not %s" % expected)
        os.replace(str(temp), str(path))
    finally:
        temp.unlink(missing_ok=True)
    if sha256_file(path) != expected:
        raise ApplyError(1, "published %s hash is not %s" % (path, expected))


def _remove_lifecycle_bytecode(runtime):
    cache = Path(runtime) / "bridge" / "__pycache__"
    if not cache.is_dir() or cache.is_symlink():
        return []
    removed = []
    for pattern in ("lifecycle*.pyc", "lifecycle*.pyo"):
        for item in cache.glob(pattern):
            if item.is_symlink() or not item.is_file():
                continue
            item.unlink()
            removed.append(item.name)
    return removed


def _backup_original(runtime, data, kind):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    directory = Path(runtime) / "backup" / "windows-inbox-replace" / (kind + "-" + stamp)
    target = directory / "lifecycle.py"
    _publish(target, data, sha256_bytes(data))
    body = json.dumps({
        "relative_path": RELATIVE_PATH,
        "source_commit": SOURCE_COMMIT,
        "agent_version": "1.14.1",
        "kind": kind,
        "sha256": sha256_bytes(data),
        "original_sha256": ORIGINAL_SHA256,
        "patched_sha256": PATCHED_SHA256,
    }, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _publish(directory / "manifest.json", body, sha256_bytes(body))
    return target


def _newest_original_backup(runtime):
    root = Path(runtime) / "backup" / "windows-inbox-replace"
    if not root.is_dir():
        return None
    matches = []
    for path in root.glob("*/lifecycle.py"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if sha256_bytes(data) == ORIGINAL_SHA256:
            matches.append(path)
    if not matches:
        return None
    return sorted(matches, key=lambda item: item.parent.name)[-1]


def apply_fix(runtime, dry_run=False):
    runtime = Path(runtime)
    target = _regular_file(runtime / RELATIVE_PATH, RELATIVE_PATH)
    _require_version(runtime)
    payload = _regular_file(payload_path(), "patched payload")
    payload_bytes = payload.read_bytes()
    if sha256_bytes(payload_bytes) != PATCHED_SHA256:
        raise ApplyError(2, "refusing: payload %s hash is not the patched v1.14.1 file" % payload)
    current = target.read_bytes()
    current_hash = sha256_bytes(current)
    if current_hash == PATCHED_SHA256:
        return {"status": "already-applied", "path": str(target), "sha256": current_hash}
    if current_hash != ORIGINAL_SHA256:
        raise ApplyError(
            2,
            "refusing: installed %s hash %s is not v1.14.1 (%s)" % (RELATIVE_PATH, current_hash, ORIGINAL_SHA256),
        )
    if dry_run:
        return {"status": "dry-run", "path": str(target), "from": ORIGINAL_SHA256, "to": PATCHED_SHA256}
    try:
        backup = _backup_original(runtime, current, "pre-apply")
    except ApplyError:
        raise
    except OSError as exc:
        raise ApplyError(1, "backup failed; installed file not changed: %s" % exc) from exc
    try:
        _publish(target, payload_bytes, PATCHED_SHA256)
    except Exception as exc:
        if sha256_file(target) != ORIGINAL_SHA256:
            _publish(target, current, ORIGINAL_SHA256)
        raise ApplyError(1, "apply failed and the installed file was left on the original hash: %s" % exc) from exc
    removed = _remove_lifecycle_bytecode(runtime)
    return {
        "status": "applied",
        "path": str(target),
        "backup": str(backup),
        "sha256": PATCHED_SHA256,
        "removed_bytecode": removed,
    }


def rollback(runtime, dry_run=False):
    runtime = Path(runtime)
    target = _regular_file(runtime / RELATIVE_PATH, RELATIVE_PATH)
    _require_version(runtime)
    current_hash = sha256_file(target)
    if current_hash == ORIGINAL_SHA256:
        return {"status": "already-original", "path": str(target), "sha256": current_hash}
    if current_hash != PATCHED_SHA256:
        raise ApplyError(
            2,
            "refusing: installed %s hash %s is neither the patched nor the original file" % (RELATIVE_PATH, current_hash),
        )
    backup = _newest_original_backup(runtime)
    embedded = embedded_original_path()
    if backup is not None:
        source = backup.read_bytes()
        source_label = str(backup)
    elif embedded is not None:
        source = embedded.read_bytes()
        source_label = str(embedded)
    else:
        raise ApplyError(2, "refusing: no backup or embedded original lifecycle.py is available")
    if sha256_bytes(source) != ORIGINAL_SHA256:
        raise ApplyError(2, "refusing: rollback source %s hash is not the v1.14.1 original" % source_label)
    if dry_run:
        return {"status": "dry-run-rollback", "path": str(target), "source": source_label}
    patched_copy = _backup_original(runtime, target.read_bytes(), "pre-rollback")
    try:
        _publish(target, source, ORIGINAL_SHA256)
    except Exception as exc:
        if sha256_file(target) != PATCHED_SHA256:
            _publish(target, Path(patched_copy).read_bytes(), PATCHED_SHA256)
        raise ApplyError(1, "rollback failed and the installed file was left on the patched hash: %s" % exc) from exc
    _remove_lifecycle_bytecode(runtime)
    return {
        "status": "rolled-back",
        "path": str(target),
        "source": source_label,
        "patched_backup": str(patched_copy),
        "sha256": ORIGINAL_SHA256,
    }


def default_runtime():
    return Path.home() / ".resolve-ai-bridge"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=None,
                        help="Installed runtime. Default: ~/.resolve-ai-bridge")
    parser.add_argument("--dry-run", action="store_true", help="Validate hashes and print the action without writing")
    parser.add_argument("--rollback", action="store_true", help="Restore the v1.14.1 lifecycle.py bytes")
    args = parser.parse_args(argv)
    runtime = args.runtime if args.runtime is not None else default_runtime()
    try:
        result = rollback(runtime, dry_run=args.dry_run) if args.rollback else apply_fix(runtime, dry_run=args.dry_run)
    except ApplyError as exc:
        print(exc, file=sys.stderr)
        return exc.code
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] == "applied":
        print("Restart the MCP client so Python reloads bridge/lifecycle.py.")
        print("Resolve and the Lua worker do not load this file and can keep running.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
