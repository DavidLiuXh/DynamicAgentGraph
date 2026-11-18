"""Private resume state. Public run records remain redacted."""

import json
import os
from contextlib import contextmanager

from ..execution.errors import RunFailure
from ..execution.privacy import contains_sensitive


@contextmanager
def run_lock(path):
    try:
        import fcntl
    except ImportError as exc:
        raise RunFailure("RESUME_UNAVAILABLE", "Run recovery requires POSIX file locks",
                         phase="reception") from exc
    try:
        fd = os.open(path / ".resume.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise RunFailure("RESUME_UNAVAILABLE", "Run directory is unavailable", phase="reception") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunFailure("RUN_BUSY", "Run is already active", phase="reception") from exc
        yield
    finally:
        os.close(fd)


class Checkpoint:
    name = "checkpoint.json"
    clock_name = "checkpoint-clock.json"

    def __init__(self, recorder, document):
        self.recorder = recorder
        self.document = document

    def save(self, **values):
        document = {**self.document, **values}
        if contains_sensitive(document, self.recorder.config.sensitive_values):
            raise RunFailure("CHECKPOINT_RESTRICTED", "Checkpoint contains restricted data", phase="recording")
        self.recorder.write(self.name, document, exact=True)
        self.document = document

    def save_clock(self, clock):
        self.recorder.write(self.clock_name, clock, exact=True)

    @classmethod
    def load(cls, recorder):
        try:
            path = recorder.path / cls.name
            if path.is_symlink():
                raise ValueError
            with path.open("rb") as stream:
                document = json.load(stream)
            if document["version"] != 1 or document["run_id"] != recorder.run_id:
                raise ValueError
            clock = recorder.path / cls.clock_name
            if clock.exists():
                if clock.is_symlink():
                    raise ValueError
                with clock.open("rb") as stream:
                    sampled = json.load(stream)
                budget = document["budget"]
                budget["elapsed_seconds"] = max(budget["elapsed_seconds"], sampled["elapsed_seconds"])
                budget["remaining_seconds"] = min(budget["remaining_seconds"], sampled["remaining_seconds"])
            return cls(recorder, document)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RunFailure("RESUME_UNAVAILABLE", "No compatible checkpoint exists", phase="reception") from exc
