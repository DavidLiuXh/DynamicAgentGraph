"""Ready-to-register local, browser, search, and web tools."""

from .local import browser_open_local_page_tool, file_read_text_tool, file_write_text_tool
from .tavily import tavily_search_tool
from .web import web_fetch_tool

__all__ = [
    "browser_open_local_page_tool",
    "file_read_text_tool",
    "file_write_text_tool",
    "tavily_search_tool",
    "web_fetch_tool",
]
