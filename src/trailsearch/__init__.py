"""TrailSearch provides self-hosted web search, crawl, and content extraction."""

__version__ = "1.0.0"
__author__ = "TrailSearch Contributors"

from trailsearch.config import get_config_info
from trailsearch.crawler import WebCrawler
from trailsearch.logger import setup_logger

__all__ = ["WebCrawler", "get_config_info", "setup_logger", "__version__"]
