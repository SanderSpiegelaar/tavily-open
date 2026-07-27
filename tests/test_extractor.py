"""
Tests for lightweight HTTP extraction helpers.
"""

from trailsearch.extractor import (
    extract_markdown_from_html,
    is_content_usable,
    looks_like_shell_page,
)


def test_extract_markdown_from_html_returns_content():
    """HTML extractor should return readable markdown for simple article pages."""
    html = """
    <html>
        <body>
            <article>
                <h1>Sample Title</h1>
                <p>This is a test article with enough readable content to be extracted.</p>
                <p>It includes multiple sentences so the extractor has meaningful text.</p>
            </article>
        </body>
    </html>
    """

    result = extract_markdown_from_html(html, "https://example.com/article")

    assert result is not None
    assert "Sample Title" in result


def test_looks_like_shell_page_detects_client_rendered_shell():
    """Shell-page heuristics should catch sparse SPA placeholders."""
    html = """
    <html>
        <body>
            <div id="root"></div>
            <script>window.__INITIAL_STATE__ = {};</script>
        </body>
    </html>
    """

    assert looks_like_shell_page(html) is True


def test_is_content_usable_enforces_min_length():
    """Content quality check should reject very short responses."""
    assert is_content_usable("short", 20) is False
    assert is_content_usable("This is long enough content.", 10) is True
