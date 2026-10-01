import asyncio
import time
from datetime import UTC, datetime

import httpx
import pytest

from dynamic_graph import CancellationToken
from dynamic_graph.contracts import CallContext, ConfigurationError
from dynamic_graph.execution.errors import ToolCallError
from dynamic_graph.graph.schemas import validate_value
from dynamic_graph.tools import web_fetch_tool

PAGE = '<html><h1>Catalog</h1><a href="/item">A &amp; B</a><p>Description</p></html>'


def context():
    return CallContext("test", "web", 1, time.monotonic() + 10, CancellationToken())


@pytest.fixture
def http_mock(monkeypatch):
    original = httpx.AsyncClient
    requests = []

    def install(handler):
        requests.clear()

        def respond(request):
            requests.append(request)
            return handler(request)

        monkeypatch.setattr(
            "dynamic_graph.tools.web.httpx.AsyncClient",
            lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
        )
        return requests

    return install


async def test_fetch_preserves_html_and_source_metadata(http_mock):
    http_mock(lambda _: httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    tool = web_fetch_tool()
    result = await tool.handler({"url": "https://example.com/list"}, context())
    validate_value(tool.output_schema, result)
    assert result["content"] == PAGE
    assert result["url"] == "https://example.com/list"
    assert result["content_type"] == "text/html"
    assert datetime.fromisoformat(result["fetched_at"]).utcoffset() == datetime.now(UTC).utcoffset()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/page",
        "https://another.example/page",
        "http://127.0.0.1:8080/page",
        "https://example.com:8443/page",
    ],
)
async def test_fetch_has_no_domain_or_port_allowlist(http_mock, url):
    requests = http_mock(lambda _: httpx.Response(200, text="text"))
    tool = web_fetch_tool()
    result = await tool.handler({"url": url}, context())
    validate_value(tool.output_schema, result)
    assert result["content"] == "text" and len(requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com/page",
        "file:///tmp/page.html",
        "javascript:alert(1)",
        "ftp://example.com",
        "https://user:password@github.com",
        "https://example.com:invalid",
        "https:///page",
    ],
)
async def test_non_http_or_invalid_urls_never_make_requests(http_mock, url):
    requests = http_mock(lambda _: httpx.Response(200))
    with pytest.raises(ToolCallError):
        await web_fetch_tool().handler({"url": url}, context())
    assert not requests


async def test_cross_domain_redirect_is_followed_and_final_url_returned(http_mock):
    requests = http_mock(
        lambda request: (
            httpx.Response(302, headers={"location": "https://other.example/page"})
            if request.url.host == "example.com"
            else httpx.Response(200, text="redirected")
        )
    )
    result = await web_fetch_tool().handler({"url": "https://example.com"}, context())
    assert result["url"] == "https://other.example/page"
    assert result["content"] == "redirected" and len(requests) == 2


async def test_redirect_limit_and_non_http_redirect(http_mock):
    requests = http_mock(lambda _: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(ToolCallError, match="Redirect limit"):
        await web_fetch_tool().handler({"url": "https://example.com"}, context())
    assert len(requests) == 4
    requests = http_mock(
        lambda _: httpx.Response(302, headers={"location": "file:///tmp/page.html"})
    )
    with pytest.raises(ToolCallError):
        await web_fetch_tool().handler({"url": "https://example.com"}, context())
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status, retryable", [(401, False), (404, False), (429, True), (503, True)]
)
async def test_http_errors_classify_retry_without_provider_body(http_mock, status, retryable):
    http_mock(lambda _: httpx.Response(status, text="private provider text"))
    with pytest.raises(ToolCallError) as error:
        await web_fetch_tool().handler({"url": "https://example.com"}, context())
    assert error.value.retryable is retryable
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "headers, content",
    [({"content-type": "image/png"}, b"abc"), ({"content-type": "text/plain"}, b"abcdef")],
)
async def test_binary_and_oversized_responses_are_rejected(http_mock, headers, content):
    http_mock(lambda _: httpx.Response(200, headers=headers, content=content))
    with pytest.raises(ToolCallError):
        await web_fetch_tool(max_bytes=5).handler({"url": "https://example.com"}, context())


async def test_deadline_and_cancel_stop_before_http(http_mock):
    requests = http_mock(lambda _: httpx.Response(200, text="unused"))
    ctx = context()
    ctx.cancellation_token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await web_fetch_tool().handler({"url": "https://example.com"}, ctx)
    ctx = CallContext("test", "web", 1, time.monotonic() - 1, CancellationToken())
    with pytest.raises(TimeoutError):
        await web_fetch_tool().handler({"url": "https://example.com"}, ctx)
    assert not requests


def test_invalid_byte_limit_is_rejected():
    with pytest.raises(ConfigurationError):
        web_fetch_tool(max_bytes=0)
