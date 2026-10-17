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
import re
import tempfile
import uuid


REQUIRED = {"source", "at", "universe", "received", "usable", "complete",
            "regular", "rows", "benchmarks", "errors", "synthetic"}
CAPTURE_PHASES = {"STARTED", "SCAN_ARCHIVED", "FEED_ERROR", "RECORD_ERROR", "ENGINE_ERROR"}
_SCAN_FILE = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}-[0-9a-f]{64}\.json\.gz\Z")
_EXCEPTION_TYPE = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,79}\Z")


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
    metadata = {"scanStartedAt", "directoryCompletedAt", "directory",
                "directorySha256", "batches"}
    present = metadata & scan.keys()
    if present and present != metadata:
        raise ValueError("Scan provenance metadata is incomplete")
    if present:
        try:
            started = datetime.fromisoformat(scan["scanStartedAt"])
            directory_ended = datetime.fromisoformat(scan["directoryCompletedAt"])
            if any(t.tzinfo is None or t.utcoffset() is None for t in (started, directory_ended)):
                raise ValueError("Scan provenance times require timezones")
            if not started <= directory_ended <= stamp:
                raise ValueError("Scan provenance times are out of order")
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid scan provenance time") from exc
        directory = scan["directory"]
        batches = scan["batches"]
        if (not isinstance(directory, list) or not isinstance(batches, list) or
                any(not isinstance(item, dict) or not all(isinstance(item.get(k), str)
                    for k in ("code", "name", "exchange", "securityType")) for item in directory)):
            raise ValueError("Invalid scan directory or batches")
        directory_bytes = json.dumps(directory, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")
        if hashlib.sha256(directory_bytes).hexdigest() != scan["directorySha256"]:
            raise ValueError("Scan directory checksum mismatch")
        previous_offset = 0
        for batch in batches:
            if (not isinstance(batch, dict) or any(type(batch.get(k)) is not int or batch[k] < 0
                    for k in ("offset", "requested", "received", "apiCalls")) or
                    batch["offset"] != previous_offset or batch["requested"] < batch["received"] or
                    batch["requested"] > 400):
                raise ValueError("Invalid scan batch range")
            try:
                batch_started = datetime.fromisoformat(batch["startedAt"])
                batch_ended = datetime.fromisoformat(batch["completedAt"])
                if (any(t.tzinfo is None or t.utcoffset() is None for t in (batch_started, batch_ended))
                        or not directory_ended <= batch_started <= batch_ended <= stamp):
                    raise ValueError("Invalid scan batch time")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Invalid scan batch time") from exc
            previous_offset += batch["requested"]
        if previous_offset > scan["universe"]:
            raise ValueError("Scan batches exceed listed universe")


def _publish_payload(payload, directory, recorded):
    """Publish a checksum-named gzip JSON file without replacing existing data."""
    directory = Path(directory)
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


def record_scan(scan, directory):
    """Atomically publish a unique scan; the filename hashes compressed bytes."""
    _check(scan)
    recorded = datetime.now(timezone.utc)
    payload = dict(scan)
    payload["schemaVersion"] = 2 if "directory" in scan else 1
    payload["recordedAt"] = recorded.isoformat()
    return _publish_payload(payload, directory, recorded)


def _check_capture_event(event):
    if not isinstance(event, dict) or type(event.get("schemaVersion")) is not int or event["schemaVersion"] != 1:
        raise ValueError("Invalid capture event schema")
    phase = event.get("phase")
    expected = {"schemaVersion", "attemptId", "startedAt", "recordedAt", "phase"}
    if phase == "SCAN_ARCHIVED":
        expected.add("scanFile")
        if not isinstance(event.get("scanFile"), str) or not _SCAN_FILE.fullmatch(event["scanFile"]):
            raise ValueError("Invalid archived scan reference")
    elif phase in ("FEED_ERROR", "RECORD_ERROR", "ENGINE_ERROR"):
        expected.add("exceptionType")
        if (not isinstance(event.get("exceptionType"), str) or
                not _EXCEPTION_TYPE.fullmatch(event["exceptionType"])):
            raise ValueError("Invalid capture exception type")
    if phase not in CAPTURE_PHASES or event.keys() != expected:
        raise ValueError("Invalid capture event fields")
    if not isinstance(event["attemptId"], str) or not re.fullmatch(r"[0-9a-f]{32}", event["attemptId"]):
        raise ValueError("Invalid capture attempt identifier")
    for key in ("startedAt", "recordedAt"):
        if not isinstance(event[key], str):
            raise ValueError("Invalid capture event time")
        try:
            stamp = datetime.fromisoformat(event[key])
        except ValueError as exc:
            raise ValueError("Invalid capture event time") from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("Capture event time requires a timezone")


def record_capture_event(event, directory):
    """Record one safe capture phase under ``scan_dir/attempts``.

    The caller supplies only allowed metadata. Raw exception messages and feed
    responses are intentionally rejected by the strict event schema.
    """
    recorded = datetime.now(timezone.utc)
    payload = {**event, "schemaVersion": 1, "recordedAt": recorded.isoformat()}
    _check_capture_event(payload)
    return _publish_payload(payload, directory, recorded)


def _read_hashed_gzip(path):
    path = Path(path)
    digest = path.name.removesuffix(".json.gz").rsplit("-", 1)[-1]
    body = path.read_bytes()
    if not path.name.endswith(".json.gz") or len(digest) != 64 or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError(f"Record checksum mismatch: {path.name}")
    return json.loads(gzip.decompress(body).decode("utf-8"))


def read_capture_event(path):
    """Verify the event checksum and strict metadata schema."""
    event = _read_hashed_gzip(path)
    _check_capture_event(event)
    return event


def read_record(path):
    """Verify the filename digest before returning an archived scan."""
    scan = _read_hashed_gzip(path)
    _check(scan)
    return scan
