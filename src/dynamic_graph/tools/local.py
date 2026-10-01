"""Bounded local text operations and opening HTML in the default browser."""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from ..capabilities.registry import ToolDefinition
from ..contracts import ConfigurationError
from ..execution.errors import ToolCallError


def _root_directory(root):
    try:
        root = Path(root).resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        raise ConfigurationError("Tool root must be an existing directory") from None
    if not root.is_dir():
        raise ConfigurationError("Tool root must be an existing directory")
    return root


def _target(root, path):
    try:
        target = (root / path).resolve()
        if not target.is_relative_to(root) or target == root:
            raise ToolCallError("Path is outside the permitted directory")
        return target
    except (OSError, ValueError, RuntimeError):
        raise ToolCallError("Invalid local path") from None


def _check_context(context):
    if context.cancellation_token.cancelled:
        raise asyncio.CancelledError
    if time.monotonic() >= context.deadline:
        raise TimeoutError("Local operation deadline exceeded")


def file_read_text_tool(root="/tmp", *, max_bytes=1_048_576):
    root = _root_directory(root)
    if max_bytes < 1:
        raise ConfigurationError("max_bytes must be positive")

    async def read(data, context):
        _check_context(context)
        target = _target(root, data["path"])
        try:
            if not target.is_file():
                raise ToolCallError("Text file does not exist")
            with target.open("rb") as stream:
                content = stream.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ToolCallError("Text file exceeds byte limit")
            text = content.decode("utf-8")
        except (OSError, UnicodeError):
            raise ToolCallError("Cannot read UTF-8 text file") from None
        _check_context(context)
        return {"path": str(target), "content": text}

    return ToolDefinition(
        "file.read_text",
        "1.0.0",
        f"Read a UTF-8 text file under {root}; maximum {max_bytes} bytes.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        read,
    )


def file_write_text_tool(root="/tmp", *, max_bytes=1_048_576):
    root = _root_directory(root)
    if max_bytes < 1:
        raise ConfigurationError("max_bytes must be positive")

    async def write(data, context):
        _check_context(context)
        target = _target(root, data["path"])
        content = data["content"].encode("utf-8")
        if len(content) > max_bytes:
            raise ToolCallError("Text exceeds byte limit")
        temporary = None
        try:
            if not target.parent.is_dir():
                raise ToolCallError("Parent directory does not exist")
            descriptor, temporary = tempfile.mkstemp(prefix=".dynamic-graph-", dir=target.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _check_context(context)
            if data.get("overwrite", False):
                os.replace(temporary, target)
            else:
                # Linking is atomic and refuses an existing destination, including races.
                os.link(temporary, target)
            return {"path": str(target), "bytes_written": len(content)}
        except FileExistsError:
            raise ToolCallError("File already exists; explicit overwrite is required") from None
        except OSError:
            raise ToolCallError("Cannot write text file") from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    return ToolDefinition(
        "file.write_text",
        "1.0.0",
        f"Atomically write UTF-8 text under {root}; parent must exist; maximum {max_bytes} bytes. "
        "Existing files require overwrite=true. This is a side effect with no automatic retries.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "bytes_written": {"type": "integer", "minimum": 0},
            },
            "required": ["path", "bytes_written"],
            "additionalProperties": False,
        },
        write,
        read_only=False,
        idempotent=False,
        reentrant=False,
    )


def browser_open_local_page_tool(root="/tmp"):
    root = _root_directory(root)

    async def open_page(data, context):
        _check_context(context)
        target = _target(root, data["path"])
        if not target.is_file() or target.suffix.lower() not in {".html", ".htm"}:
            raise ToolCallError("An existing local HTML file is required")
        uri = target.as_uri()
        if sys.platform == "win32":
            try:
                os.startfile(uri)
            except OSError:
                raise ToolCallError("Browser launch failed") from None
        else:
            command = "open" if sys.platform == "darwin" else "xdg-open"
            try:
                process = await asyncio.create_subprocess_exec(
                    command,
                    uri,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError:
                raise ToolCallError("Browser launcher is unavailable") from None
            try:
                async with asyncio.timeout_at(context.deadline):
                    code = await process.wait()
            except (TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                raise
            if code != 0:
                raise ToolCallError("Browser launch failed")
        return {"path": str(target), "uri": uri, "launch_requested": True}

    return ToolDefinition(
        "browser.open_local_page",
        "1.0.0",
        f"Ask the OS default browser to open an existing .html/.htm file under {root}. "
        "Depends on the file producer. Success acknowledges launch, not rendering. No automatic retry.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "uri": {"type": "string"},
                "launch_requested": {"type": "boolean"},
            },
            "required": ["path", "uri", "launch_requested"],
            "additionalProperties": False,
        },
        open_page,
        read_only=False,
        idempotent=False,
        reentrant=False,
    )
