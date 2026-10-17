"""Append-only, content-verifiable records of complete PaperDesk scan responses.

Each gzip-compressed UTF-8 JSON file is one scan, including rejected/incomplete
scans. A SHA-256 of the entire compressed file is embedded in its name.
"""

from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid


REQUIRED = {"source", "at", "universe", "received", "usable", "complete",
            "regular", "rows", "benchmarks", "errors", "synthetic"}


def _check(scan):
    if not isinstance(scan, dict) or REQUIRED - scan.keys():
        raise ValueError("Scan record lacks required top-level fields")
    if (not isinstance(scan["source"], str) or not isinstance(scan["at"], str)
            or not isinstance(scan["rows"], list)
            or not isinstance(scan["benchmarks"], dict)
            or not isinstance(scan["errors"], list)):
        raise ValueError("Scan record has invalid field types")
    for key in ("universe", "received", "usable"):
        if type(scan[key]) is not int or scan[key] < 0:
            raise ValueError(f"Invalid scan count: {key}")
    for key in ("complete", "regular", "synthetic"):
        if type(scan[key]) is not bool:
            raise ValueError(f"Invalid scan flag: {key}")
    # A source timestamp must be explicit and timezone-aware. Do not substitute
    # the archive timestamp for a missing or ambiguous quote observation time.
    try:
        stamp = datetime.fromisoformat(scan["at"])
    except ValueError as exc:
        raise ValueError("Invalid scan observation time") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Scan observation time requires a timezone")


def record_scan(scan, directory):
    """Atomically publish a unique compressed scan JSON; return its path.

    The final filename is the manifest: its last component before ``.json`` is
    the full SHA-256 digest of the compressed bytes on disk. A hard link publishes
    the completed temp file with no replacement of an existing record.
    """
    _check(scan)
    directory = Path(directory)
    recorded = datetime.now(timezone.utc)
    payload = dict(scan)
    payload["schemaVersion"] = 1
    payload["recordedAt"] = recorded.isoformat()
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    body = gzip.compress(raw, compresslevel=6, mtime=0)
    digest = hashlib.sha256(body).hexdigest()
    name = f"{recorded:%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex}-{digest}.json.gz"
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / name
    fd, temporary = tempfile.mkstemp(prefix=".scan-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, final)  # EEXIST protects previously committed data.
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some filesystems do not support syncing directories. The record
            # itself is complete and already durably synced when this occurs.
            pass
    finally:
        Path(temporary).unlink(missing_ok=True)
    return final


def read_record(path):
    """Verify the filename digest before returning an archived scan."""
    path = Path(path)
    digest = path.name.removesuffix(".json.gz").rsplit("-", 1)[-1]
    body = path.read_bytes()
    if not path.name.endswith(".json.gz") or len(digest) != 64 or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError(f"Scan record checksum mismatch: {path.name}")
    scan = json.loads(gzip.decompress(body).decode("utf-8"))
    _check(scan)
    return scan
