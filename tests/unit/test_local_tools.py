import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from dynamic_graph import CancellationToken
from dynamic_graph.contracts import CallContext, ConfigurationError
from dynamic_graph.execution.errors import ToolCallError
from dynamic_graph.graph.schemas import validate_value
from dynamic_graph.tools import (
    browser_open_local_page_tool,
    file_read_text_tool,
    file_write_text_tool,
)


def context():
    return CallContext("test", "local", 1, time.monotonic() + 10, CancellationToken())


async def test_default_directory_is_tmp(monkeypatch):
    calls = []
    monkeypatch.setattr("dynamic_graph.tools.local.sys.platform", "win32")
    monkeypatch.setattr("dynamic_graph.tools.local.os.startfile", calls.append, raising=False)
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="dynamic-graph-default-") as directory:
        path = Path(directory) / "page.html"
        write, read, browser = (
            file_write_text_tool(),
            file_read_text_tool(),
            browser_open_local_page_tool(),
        )
        await write.handler({"path": str(path), "content": "<html>default</html>"}, context())
        assert (await read.handler({"path": str(path)}, context()))[
            "content"
        ] == "<html>default</html>"
        await browser.handler({"path": str(path)}, context())
        assert calls == [path.resolve().as_uri()]


async def test_utf8_write_read_and_explicit_overwrite(tmp_path):
    write = file_write_text_tool(tmp_path)
    read = file_read_text_tool(tmp_path)
    value = await write.handler({"path": "github.html", "content": "你好"}, context())
    validate_value(write.output_schema, value)
    assert value["bytes_written"] == 6
    assert write.read_only is False and write.idempotent is False
    assert (await read.handler({"path": value["path"]}, context()))["content"] == "你好"
    with pytest.raises(ToolCallError, match="overwrite"):
        await write.handler({"path": "github.html", "content": "other"}, context())
    assert (tmp_path / "github.html").read_text() == "你好"
    await write.handler({"path": "github.html", "content": "new", "overwrite": True}, context())
    assert (tmp_path / "github.html").read_text() == "new"
    assert not list(tmp_path.glob(".dynamic-graph-*"))


@pytest.mark.parametrize(
    "factory", [file_read_text_tool, file_write_text_tool, browser_open_local_page_tool]
)
@pytest.mark.parametrize("path", ["../outside.html", "link.html", "", "."])
async def test_paths_and_symlinks_cannot_escape_root(tmp_path, factory, path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.html"
    outside.write_text("unchanged")
    (root / "link.html").symlink_to(outside)
    tool = factory(root)
    with pytest.raises(ToolCallError, match="outside"):
        await tool.handler({"path": path, "content": "changed", "overwrite": True}, context())
    assert outside.read_text() == "unchanged"


@pytest.mark.parametrize("operation", ["read", "write"])
async def test_limits_are_utf8_byte_limits(tmp_path, operation):
    (tmp_path / "large.txt").write_text("你好")
    tool = (file_read_text_tool if operation == "read" else file_write_text_tool)(
        tmp_path, max_bytes=5
    )
    with pytest.raises(ToolCallError, match="byte limit"):
        await tool.handler({"path": "large.txt", "content": "你好", "overwrite": True}, context())
    assert (tmp_path / "large.txt").read_text() == "你好"


@pytest.mark.parametrize(
    "factory", [file_read_text_tool, file_write_text_tool, browser_open_local_page_tool]
)
@pytest.mark.parametrize("stop", ["deadline", "cancel"])
async def test_local_operations_stop_before_effects(tmp_path, factory, stop):
    ctx = context()
    if stop == "deadline":
        object.__setattr__(ctx, "deadline", time.monotonic() - 1)
    else:
        ctx.cancellation_token.cancel()
    with pytest.raises(TimeoutError if stop == "deadline" else asyncio.CancelledError):
        await factory(tmp_path).handler({"path": "new.html", "content": "new"}, ctx)
    assert not (tmp_path / "new.html").exists()


async def test_missing_binary_and_missing_parent_are_rejected(tmp_path):
    with pytest.raises(ToolCallError, match="does not exist"):
        await file_read_text_tool(tmp_path).handler({"path": "missing"}, context())
    (tmp_path / "binary").write_bytes(b"\xff")
    with pytest.raises(ToolCallError, match="UTF-8"):
        await file_read_text_tool(tmp_path).handler({"path": "binary"}, context())
    with pytest.raises(ToolCallError, match="Parent"):
        await file_write_text_tool(tmp_path).handler(
            {"path": "missing/new", "content": "x"}, context()
        )
    with pytest.raises(ConfigurationError):
        file_write_text_tool(tmp_path / "missing")


@pytest.mark.parametrize("platform, expected", [("darwin", "open"), ("linux", "xdg-open")])
async def test_browser_uses_an_argument_vector_and_encoded_file_uri(
    tmp_path, monkeypatch, platform, expected
):
    path = tmp_path / "space ; file.html"
    path.write_text("<html></html>")
    calls = []

    class Process:
        returncode = 0

        async def wait(self):
            return 0

    async def launch(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr("dynamic_graph.tools.local.sys.platform", platform)
    monkeypatch.setattr("dynamic_graph.tools.local.asyncio.create_subprocess_exec", launch)
    tool = browser_open_local_page_tool(tmp_path)
    result = await tool.handler({"path": str(path)}, context())
    validate_value(tool.output_schema, result)
    assert calls == [(expected, path.as_uri())]
    assert result["launch_requested"] is True and tool.idempotent is False


async def test_windows_browser_and_missing_html(tmp_path, monkeypatch):
    path = tmp_path / "page.html"
    path.write_text("<html></html>")
    calls = []
    monkeypatch.setattr("dynamic_graph.tools.local.sys.platform", "win32")
    monkeypatch.setattr("dynamic_graph.tools.local.os.startfile", calls.append, raising=False)
    tool = browser_open_local_page_tool(tmp_path)
    await tool.handler({"path": "page.html"}, context())
    assert calls == [path.as_uri()]
    with pytest.raises(ToolCallError, match="existing local HTML"):
        await tool.handler({"path": "missing.html"}, context())
    (tmp_path / "command.sh").write_text("echo nope")
    with pytest.raises(ToolCallError, match="HTML"):
        await tool.handler({"path": "command.sh"}, context())


async def test_browser_timeout_kills_launcher(tmp_path, monkeypatch):
    (tmp_path / "page.html").write_text("<html></html>")

    class Process:
        returncode = None
        killed = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            if self.returncode is None:
                await asyncio.sleep(10)
            return self.returncode

    process = Process()

    async def launch(*args, **kwargs):
        return process

    monkeypatch.setattr("dynamic_graph.tools.local.sys.platform", "linux")
    monkeypatch.setattr("dynamic_graph.tools.local.asyncio.create_subprocess_exec", launch)
    ctx = CallContext("test", "browser", 1, time.monotonic() + 0.02, CancellationToken())
    with pytest.raises(TimeoutError):
        await browser_open_local_page_tool(tmp_path).handler({"path": "page.html"}, ctx)
    assert process.killed
