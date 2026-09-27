import csv
import hashlib
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..execution.errors import RunFailure
from ..execution.privacy import contains_sensitive, redact_sensitive
from ..graph.schemas import canonical


def restrict_windows_acl(path):
    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    rows = list(csv.reader(identity.stdout.strip().splitlines()))
    sid = rows[-1][-1].strip() if rows else ""
    if not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise ValueError("Could not resolve current Windows user SID")
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


class Recorder:
    def __init__(self, config, run_id):
        self.config, self.run_id = config, run_id
        self.path = Path(config.runs_dir).absolute() / run_id
        self.seq = 0
        self.manifest = {
            "run_id": run_id,
            "schema_version": "1.0",
            "phase": "reception",
            "recording": "complete",
            "terminal": False,
            "compilations": [],
        }

    def redact(self, value):
        result = redact_sensitive(value, self.config.sensitive_values)
        if self.config.redactor:
            result = self.config.redactor(result)
        return result

    def create(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.mkdir(mode=0o700)
            if os.name == "nt":
                restrict_windows_acl(self.path)
            self.write("manifest.json", self.manifest)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise RunFailure(
                "RECORDING_FAILED",
                "Cannot create run directory",
                phase="reception",
                details={"errno": getattr(exc, "errno", None), "operation": "create_directory"},
            ) from exc

    def write(self, name, value, *, exact=False):
        value = value if exact else self.redact(value)
        data = canonical(value)
        return self._write_bytes(name, data)

    def write_text(self, name, value):
        return self._write_bytes(name, self.redact(value).encode("utf-8"))

    def _write_bytes(self, name, data):
        path = self.path / name
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                parent_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
        except OSError as exc:
            raise RunFailure(
                "RECORDING_FAILED",
                "Cannot atomically save run record",
                phase="recording",
                details={"errno": exc.errno, "operation": "atomic_write"},
            ) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return hashlib.sha256(data).hexdigest()

    def phase(self, phase):
        self.manifest["phase"] = phase
        self.write("manifest.json", self.manifest)

    def event(self, event_type, *, node_id=None, attempt=None, payload_ref=None, error_code=None):
        self.seq += 1
        value = {
            "schema_version": "1.0",
            "seq": self.seq,
            "run_id": self.run_id,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "phase": self.manifest["phase"],
            "event_type": event_type,
            "node_id": node_id,
            "attempt": attempt,
            "payload_ref": payload_ref,
            "error_code": error_code,
        }
        try:
            fd = os.open(self.path / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as handle:
                handle.write(canonical(value) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise RunFailure(
                "RECORDING_FAILED",
                "Cannot append run event",
                phase="recording",
                details={"errno": exc.errno, "operation": "append_event"},
            ) from exc

    def save_graph(self, spec, attempt):
        document = spec.document()
        if contains_sensitive(document, self.config.sensitive_values):
            raise RunFailure(
                "GRAPH_SENSITIVE_LITERAL",
                "Graph contains a restricted literal; use input references",
                phase="validation",
            )
        digest = self.write("graph.json", document, exact=True)
        self.manifest["graph_hash"] = digest
        self.manifest["compilations"].append({"attempt": attempt, "graph_hash": digest})
        self.write("manifest.json", self.manifest)
        self.event("graph_saved", payload_ref="graph.json")
        return str(self.path / "graph.json"), digest
