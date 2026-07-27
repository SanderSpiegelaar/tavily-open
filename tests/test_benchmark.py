r"""
Optional crawler benchmarks for objective extraction-stage comparison.

Run a quick, lightweight pass:
    $env:TRAILSEARCH_RUN_BENCHMARK="1"
    $env:TRAILSEARCH_BENCHMARK_PRESET="fast"
    .venv\Scripts\pytest.exe tests/test_benchmark.py -m benchmark -s --no-cov

Run a short browser comparison after Chromium is installed:
    $env:TRAILSEARCH_RUN_BENCHMARK="1"
    $env:TRAILSEARCH_BENCHMARK_PRESET="quick"
    .venv\Scripts\pytest.exe tests/test_benchmark.py -m benchmark -s --no-cov

Allow the benchmark to install missing Playwright browsers:
    $env:TRAILSEARCH_BENCHMARK_INSTALL_BROWSERS="1"

Install optional Scrapling profiles:
    .venv\Scripts\pip.exe install "scrapling[fetchers]"
    .venv\Scripts\scrapling.exe install

Run all profiles, including heavier browser fallbacks:
    $env:TRAILSEARCH_RUN_BENCHMARK="1"
    $env:TRAILSEARCH_BENCHMARK_PRESET="all"
    $env:TRAILSEARCH_BENCHMARK_ROUNDS="3"
    .venv\Scripts\pytest.exe tests/test_benchmark.py -m benchmark -s --no-cov
"""

import asyncio
import json
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

import aiohttp
import httpx
import pytest
from aiohttp import web

from trailsearch.anti_crawl import AntiCrawlConfig
from trailsearch.crawler import WebCrawler
from trailsearch.extractor import fetch_with_http_extractor
from trailsearch.reader import build_reader_api_url

RUN_BENCHMARK = os.getenv("TRAILSEARCH_RUN_BENCHMARK", "").lower() in {"1", "true", "yes"}
RUN_REAL_BENCHMARK = os.getenv("TRAILSEARCH_RUN_REAL_BENCHMARK", "").lower() in {
    "1",
    "true",
    "yes",
}
BENCHMARK_ROUNDS = int(os.getenv("TRAILSEARCH_BENCHMARK_ROUNDS", "2"))
BENCHMARK_OUTPUT = os.getenv("TRAILSEARCH_BENCHMARK_OUTPUT", "benchmark-results.json")
BENCHMARK_PRESET = os.getenv("TRAILSEARCH_BENCHMARK_PRESET", "fast").lower().strip()
BENCHMARK_INSTALL_BROWSERS = os.getenv(
    "TRAILSEARCH_BENCHMARK_INSTALL_BROWSERS",
    "",
).lower() in {"1", "true", "yes"}
REAL_BENCHMARK_ROUNDS = int(os.getenv("TRAILSEARCH_REAL_BENCHMARK_ROUNDS", "2"))
REAL_BENCHMARK_OUTPUT = os.getenv(
    "TRAILSEARCH_REAL_BENCHMARK_OUTPUT",
    "realworld-benchmark-results.json",
)
MIN_CONTENT_LENGTH = int(os.getenv("TRAILSEARCH_BENCHMARK_MIN_CONTENT_LENGTH", "120"))
BENCHMARK_PROFILES = {
    profile.strip()
    for profile in os.getenv("TRAILSEARCH_BENCHMARK_PROFILES", "").split(",")
    if profile.strip()
}
REAL_BENCHMARK_PROFILES = {
    profile.strip()
    for profile in os.getenv("TRAILSEARCH_REAL_BENCHMARK_PROFILES", "").split(",")
    if profile.strip()
}


@dataclass(frozen=True)
class BenchmarkCase:
    """One benchmark fixture page with expected extraction signals."""

    case_id: str
    path: str
    category: str
    capability_tags: tuple[str, ...]
    required_snippets: tuple[str, ...]
    forbidden_snippets: tuple[str, ...]
    expected_text: str = ""


@dataclass
class BenchmarkProfile:
    """A runnable extraction profile."""

    name: str
    description: str
    runner: Callable[[list[str]], Awaitable[dict[str, Any]]]


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _content_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _normalize_text(text))


def _token_metrics(content: str, expected_text: str) -> dict[str, float]:
    expected_tokens = Counter(_tokenize(expected_text))
    content_tokens = Counter(_tokenize(content))
    if not expected_tokens or not content_tokens:
        return {"text_precision": 0.0, "text_recall": 0.0, "text_f1": 0.0}

    overlap = sum(min(content_tokens[token], count) for token, count in expected_tokens.items())
    precision = overlap / sum(content_tokens.values())
    recall = overlap / sum(expected_tokens.values())
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return {
        "text_precision": round(precision, 4),
        "text_recall": round(recall, 4),
        "text_f1": round(f1, 4),
    }


def _has_snippet(content: str, snippet: str) -> bool:
    normalized_content = _normalize_text(content)
    normalized_snippet = _normalize_text(snippet)
    if normalized_snippet in normalized_content:
        return True

    snippet_tokens = _tokenize(snippet)
    content_tokens = _tokenize(content)
    if not snippet_tokens or len(snippet_tokens) > len(content_tokens):
        return False

    snippet_size = len(snippet_tokens)
    return any(
        content_tokens[index : index + snippet_size] == snippet_tokens
        for index in range(len(content_tokens) - snippet_size + 1)
    )


def _content_preview(content: str, limit: int = 320) -> str:
    preview = re.sub(r"\s+", " ", content).strip()
    return preview[:limit]


def _quality_for_content(content: str, benchmark_case: BenchmarkCase) -> dict[str, Any]:
    required_hits = [
        snippet for snippet in benchmark_case.required_snippets if _has_snippet(content, snippet)
    ]
    exact_required_hits = [
        snippet
        for snippet in benchmark_case.required_snippets
        if _normalize_text(snippet) in _normalize_text(content)
    ]
    forbidden_hits = [
        snippet
        for snippet in benchmark_case.forbidden_snippets
        if _has_snippet(content, snippet)
    ]
    required_total = len(benchmark_case.required_snippets)
    recall = len(required_hits) / required_total if required_total else 1.0
    noise_penalty = len(forbidden_hits) / max(len(benchmark_case.forbidden_snippets), 1)
    expected_text = benchmark_case.expected_text or "\n".join(benchmark_case.required_snippets)
    token_quality = _token_metrics(content, expected_text)
    content_length = _content_length(content)
    unusable_reasons = []
    if recall < 0.8:
        unusable_reasons.append("low_required_recall")
    if forbidden_hits:
        unusable_reasons.append("forbidden_noise")
    if content_length < MIN_CONTENT_LENGTH:
        unusable_reasons.append("short_content")
    usable = not unusable_reasons
    return {
        "usable": usable,
        "recall": round(recall, 4),
        "noise_penalty": round(noise_penalty, 4),
        **token_quality,
        "required_hits": required_hits,
        "exact_required_hits": exact_required_hits,
        "forbidden_hits": forbidden_hits,
        "content_length": content_length,
        "unusable_reasons": unusable_reasons,
    }


def _evaluate_results(
    cases_by_url: dict[str, BenchmarkCase],
    result_items: list[dict[str, str]],
) -> dict[str, Any]:
    by_url = {item.get("reference", ""): item.get("content", "") for item in result_items}
    per_case: list[dict[str, Any]] = []
    category_stats: dict[str, Counter] = defaultdict(Counter)
    tag_stats: dict[str, Counter] = defaultdict(Counter)

    for url, benchmark_case in cases_by_url.items():
        content = by_url.get(url, "")
        quality = _quality_for_content(content, benchmark_case) if content else {
            "usable": False,
            "recall": 0.0,
            "noise_penalty": 0.0,
            "text_precision": 0.0,
            "text_recall": 0.0,
            "text_f1": 0.0,
            "required_hits": [],
            "exact_required_hits": [],
            "forbidden_hits": [],
            "content_length": 0,
            "unusable_reasons": ["missing_content"],
        }
        per_case.append(
            {
                "case_id": benchmark_case.case_id,
                "category": benchmark_case.category,
                "capability_tags": list(benchmark_case.capability_tags),
                "found": bool(content),
                "content_preview": _content_preview(content) if content else "",
                **quality,
            }
        )

        category_stats[benchmark_case.category]["total"] += 1
        category_stats[benchmark_case.category]["found"] += int(bool(content))
        category_stats[benchmark_case.category]["usable"] += int(bool(quality["usable"]))
        category_stats[benchmark_case.category]["recall_sum"] += quality["recall"]
        category_stats[benchmark_case.category]["noise_sum"] += quality["noise_penalty"]
        category_stats[benchmark_case.category]["text_f1_sum"] += quality["text_f1"]
        category_stats[benchmark_case.category]["text_precision_sum"] += quality["text_precision"]
        for tag in benchmark_case.capability_tags:
            tag_stats[tag]["total"] += 1
            tag_stats[tag]["found"] += int(bool(content))
            tag_stats[tag]["usable"] += int(bool(quality["usable"]))
            tag_stats[tag]["recall_sum"] += quality["recall"]
            tag_stats[tag]["noise_sum"] += quality["noise_penalty"]
            tag_stats[tag]["text_f1_sum"] += quality["text_f1"]
            tag_stats[tag]["text_precision_sum"] += quality["text_precision"]

    total_cases = len(cases_by_url)
    found_count = sum(1 for item in per_case if item["found"])
    usable_count = sum(1 for item in per_case if item["usable"])
    mean_recall = statistics.mean(item["recall"] for item in per_case) if per_case else 0.0
    mean_noise = statistics.mean(item["noise_penalty"] for item in per_case) if per_case else 0.0
    mean_text_precision = statistics.mean(item["text_precision"] for item in per_case) if per_case else 0.0
    mean_text_f1 = statistics.mean(item["text_f1"] for item in per_case) if per_case else 0.0

    return {
        "found_count": found_count,
        "usable_count": usable_count,
        "success_rate": round(found_count / total_cases, 4) if total_cases else 0.0,
        "usable_rate": round(usable_count / total_cases, 4) if total_cases else 0.0,
        "mean_required_recall": round(mean_recall, 4),
        "mean_noise_penalty": round(mean_noise, 4),
        "mean_text_precision": round(mean_text_precision, 4),
        "mean_text_f1": round(mean_text_f1, 4),
        "by_category": _summarize_counter_map(category_stats),
        "by_capability": _summarize_counter_map(tag_stats),
        "cases": per_case,
    }


def _summarize_counter_map(stats: dict[str, Counter]) -> dict[str, dict[str, float | int]]:
    summary = {}
    for key, counter in stats.items():
        total = counter["total"]
        summary[key] = {
            "total": total,
            "found": counter["found"],
            "usable": counter["usable"],
            "success_rate": round(counter["found"] / total, 4) if total else 0.0,
            "usable_rate": round(counter["usable"] / total, 4) if total else 0.0,
            "mean_required_recall": round(counter["recall_sum"] / total, 4) if total else 0.0,
            "mean_noise_penalty": round(counter["noise_sum"] / total, 4) if total else 0.0,
            "mean_text_precision": round(counter["text_precision_sum"] / total, 4)
            if total
            else 0.0,
            "mean_text_f1": round(counter["text_f1_sum"] / total, 4) if total else 0.0,
        }
    return summary


def _article_expected_text(case_id: str) -> str:
    return "\n".join(
        [
            f"Benchmark Article {case_id}",
            f"GOLD-STATIC-PRIMARY_{case_id} stable article body for extraction benchmarking.",
            (
                f"GOLD-STATIC-SECONDARY_{case_id} repeated readable paragraph with enough text "
                "to pass minimum content thresholds and measure quality."
            ),
            f"GOLD-STATIC-TERTIARY_{case_id} final relevant sentence for recall scoring.",
        ]
    )


def _article_html(case_id: str) -> str:
    return f"""
    <html>
      <head><title>Benchmark Article {case_id}</title></head>
      <body>
        <header>GLOBAL-NAVIGATION-NOISE_{case_id}</header>
        <main>
          <article>
            <h1>Benchmark Article {case_id}</h1>
            <p>GOLD-STATIC-PRIMARY_{case_id} stable article body for extraction benchmarking.</p>
            <p>GOLD-STATIC-SECONDARY_{case_id} repeated readable paragraph with enough text to
            pass minimum content thresholds and measure quality.</p>
            <p>GOLD-STATIC-TERTIARY_{case_id} final relevant sentence for recall scoring.</p>
          </article>
        </main>
        <footer>GLOBAL-FOOTER-NOISE_{case_id}</footer>
      </body>
    </html>
    """


def _boilerplate_expected_text(case_id: str) -> str:
    return "\n".join(
        [
            f"Benchmark Boilerplate {case_id}",
            f"GOLD-BOILERPLATE-PRIMARY_{case_id} dense relevant content surrounded by noisy navigation.",
            f"GOLD-BOILERPLATE-SECONDARY_{case_id} another relevant sentence for precision checks.",
        ]
    )


def _boilerplate_html(case_id: str) -> str:
    nav = "".join(f"<a>GLOBAL-NAVIGATION-NOISE_{case_id}_{index}</a>" for index in range(20))
    return f"""
    <html>
      <body>
        <nav>{nav}</nav>
        <main>
          <article>
            <h1>Benchmark Boilerplate {case_id}</h1>
            <p>GOLD-BOILERPLATE-PRIMARY_{case_id} dense relevant content surrounded by noisy navigation.</p>
            <p>GOLD-BOILERPLATE-SECONDARY_{case_id} another relevant sentence for precision checks.</p>
          </article>
        </main>
        <aside>GLOBAL-ASIDE-NOISE_{case_id}</aside>
      </body>
    </html>
    """


def _docs_expected_text(case_id: str) -> str:
    return "\n".join(
        [
            f"Benchmark Docs {case_id}",
            f"GOLD-DOCS-PRIMARY_{case_id} installation guidance for crawler benchmark users.",
            f"GOLD-DOCS-CODE_{case_id} trailsearch benchmark --profile quick",
            f"GOLD-DOCS-SECONDARY_{case_id} configuration details with stable option descriptions.",
        ]
    )


def _docs_html(case_id: str) -> str:
    return f"""
    <html>
      <body>
        <aside>DOCS-SIDEBAR-NOISE_{case_id} API Reference Changelog Community</aside>
        <main>
          <article>
            <h1>Benchmark Docs {case_id}</h1>
            <p>GOLD-DOCS-PRIMARY_{case_id} installation guidance for crawler benchmark users.</p>
            <pre><code>GOLD-DOCS-CODE_{case_id} trailsearch benchmark --profile quick</code></pre>
            <p>GOLD-DOCS-SECONDARY_{case_id} configuration details with stable option descriptions.</p>
          </article>
        </main>
      </body>
    </html>
    """


def _product_expected_text(case_id: str) -> str:
    return "\n".join(
        [
            f"Benchmark Product {case_id}",
            f"GOLD-PRODUCT-NAME_{case_id} TrailSearch Test Jacket",
            f"GOLD-PRODUCT-PRICE_{case_id} 129.00",
            f"GOLD-PRODUCT-DESCRIPTION_{case_id} structured product copy with material and availability facts.",
        ]
    )


def _product_html(case_id: str) -> str:
    return f"""
    <html>
      <head>
        <script type="application/ld+json">
        {{"@type":"Product","name":"GOLD-PRODUCT-NAME_{case_id} TrailSearch Test Jacket",
        "offers":{{"price":"GOLD-PRODUCT-PRICE_{case_id} 129.00"}}}}
        </script>
      </head>
      <body>
        <header>SHOP-NAV-NOISE_{case_id} cart account sale</header>
        <main>
          <section>
            <h1>Benchmark Product {case_id}</h1>
            <p>GOLD-PRODUCT-NAME_{case_id} TrailSearch Test Jacket</p>
            <p>GOLD-PRODUCT-PRICE_{case_id} 129.00</p>
            <p>GOLD-PRODUCT-DESCRIPTION_{case_id} structured product copy with material and availability facts.</p>
          </section>
        </main>
      </body>
    </html>
    """


def _listing_expected_text(case_id: str) -> str:
    return "\n".join(
        [
            f"Benchmark Listing {case_id}",
            f"GOLD-LISTING-ITEM-A_{case_id} crawler benchmark adapter",
            f"GOLD-LISTING-ITEM-B_{case_id} reader fallback adapter",
            f"GOLD-LISTING-ITEM-C_{case_id} browser rendering adapter",
        ]
    )


def _listing_html(case_id: str) -> str:
    cards = "\n".join(
        [
            f"<li><a>GOLD-LISTING-ITEM-A_{case_id} crawler benchmark adapter</a></li>",
            f"<li><a>GOLD-LISTING-ITEM-B_{case_id} reader fallback adapter</a></li>",
            f"<li><a>GOLD-LISTING-ITEM-C_{case_id} browser rendering adapter</a></li>",
        ]
    )
    return f"""
    <html>
      <body>
        <nav>LISTING-FILTER-NOISE_{case_id} sort newest cheapest popular</nav>
        <main>
          <h1>Benchmark Listing {case_id}</h1>
          <ul>{cards}</ul>
        </main>
      </body>
    </html>
    """


def _forum_expected_text(case_id: str) -> str:
    return "\n".join(
        [
            f"Benchmark Thread {case_id}",
            f"GOLD-FORUM-QUESTION_{case_id} how should crawler fallbacks be ordered?",
            f"GOLD-FORUM-ANSWER_{case_id} use static extraction first and render only shell pages.",
            f"GOLD-FORUM-FOLLOWUP_{case_id} measure fallback hits separately from total success.",
        ]
    )


def _forum_html(case_id: str) -> str:
    return f"""
    <html>
      <body>
        <header>FORUM-HEADER-NOISE_{case_id} login subscribe promoted</header>
        <main>
          <h1>Benchmark Thread {case_id}</h1>
          <article>GOLD-FORUM-QUESTION_{case_id} how should crawler fallbacks be ordered?</article>
          <article>GOLD-FORUM-ANSWER_{case_id} use static extraction first and render only shell pages.</article>
          <article>GOLD-FORUM-FOLLOWUP_{case_id} measure fallback hits separately from total success.</article>
        </main>
      </body>
    </html>
    """


def _table_expected_text(case_id: str) -> str:
    return "\n".join(
        [
            f"Benchmark Data Table {case_id}",
            f"GOLD-TABLE-HEADER_{case_id} Profile Usable Median",
            f"GOLD-TABLE-ROW-A_{case_id} http_extractor fast static baseline",
            f"GOLD-TABLE-ROW-B_{case_id} local_playwright rendered fallback",
        ]
    )


def _table_html(case_id: str) -> str:
    return f"""
    <html>
      <body>
        <main>
          <h1>Benchmark Data Table {case_id}</h1>
          <table>
            <thead><tr><th>GOLD-TABLE-HEADER_{case_id} Profile</th><th>Usable</th><th>Median</th></tr></thead>
            <tbody>
              <tr><td>GOLD-TABLE-ROW-A_{case_id} http_extractor</td><td>fast</td><td>static baseline</td></tr>
              <tr><td>GOLD-TABLE-ROW-B_{case_id} local_playwright</td><td>rendered</td><td>fallback</td></tr>
            </tbody>
          </table>
        </main>
      </body>
    </html>
    """


def _slow_expected_text(case_id: str) -> str:
    return "\n".join(
        [
            f"Benchmark Slow {case_id}",
            f"GOLD-SLOW-PRIMARY_{case_id} delayed response content for timeout sensitivity.",
            f"GOLD-SLOW-SECONDARY_{case_id} stable delayed article body.",
        ]
    )


def _slow_html(case_id: str) -> str:
    return f"""
    <html>
      <body>
        <article>
          <h1>Benchmark Slow {case_id}</h1>
          <p>GOLD-SLOW-PRIMARY_{case_id} delayed response content for timeout sensitivity.</p>
          <p>GOLD-SLOW-SECONDARY_{case_id} stable delayed article body.</p>
        </article>
      </body>
    </html>
    """


def _shell_expected_text(case_id: str) -> str:
    return "\n".join(
        [
            f"Benchmark Shell {case_id}",
            f"GOLD-JS-PRIMARY_{case_id} rendered client-side content for browser fallback.",
            f"GOLD-JS-SECONDARY_{case_id} hydrated detail that is absent from the static DOM.",
        ]
    )


def _shell_html(case_id: str) -> str:
    return f"""
    <html>
      <head><title>Benchmark Shell {case_id}</title></head>
      <body>
        <div id="root">CLIENT-SHELL-LOADING_{case_id}</div>
        <noscript>NO-JS-FALLBACK-NOISE_{case_id}</noscript>
        <script>
          window.__INITIAL_STATE__ = {{
            title: "Benchmark Shell {case_id}",
            primary: "GOLD-JS-PRIMARY_{case_id} rendered client-side content for browser fallback.",
            secondary: "GOLD-JS-SECONDARY_{case_id} hydrated detail that is absent from the static DOM."
          }};
          setTimeout(function () {{
            var state = window.__INITIAL_STATE__;
            document.getElementById("root").innerHTML =
              "<main><article><h1>" + state.title + "</h1><p>" +
              state.primary + "</p><p>" + state.secondary + "</p></article></main>";
          }}, 25);
        </script>
      </body>
    </html>
    """


def _benchmark_cases() -> list[BenchmarkCase]:
    cases = [
        BenchmarkCase(
            case_id="static-0",
            path="/article/static-0",
            category="static_article",
            capability_tags=("static_html", "article_extraction"),
            required_snippets=(
                "GOLD-STATIC-PRIMARY_static-0",
                "GOLD-STATIC-SECONDARY_static-0",
                "GOLD-STATIC-TERTIARY_static-0",
            ),
            forbidden_snippets=("GLOBAL-NAVIGATION-NOISE_static-0", "GLOBAL-FOOTER-NOISE_static-0"),
            expected_text=_article_expected_text("static-0"),
        ),
        BenchmarkCase(
            case_id="static-1",
            path="/article/static-1",
            category="static_article",
            capability_tags=("static_html", "article_extraction"),
            required_snippets=(
                "GOLD-STATIC-PRIMARY_static-1",
                "GOLD-STATIC-SECONDARY_static-1",
                "GOLD-STATIC-TERTIARY_static-1",
            ),
            forbidden_snippets=("GLOBAL-NAVIGATION-NOISE_static-1", "GLOBAL-FOOTER-NOISE_static-1"),
            expected_text=_article_expected_text("static-1"),
        ),
        BenchmarkCase(
            case_id="boilerplate-0",
            path="/boilerplate/boilerplate-0",
            category="boilerplate_heavy",
            capability_tags=("static_html", "boilerplate_removal"),
            required_snippets=(
                "GOLD-BOILERPLATE-PRIMARY_boilerplate-0",
                "GOLD-BOILERPLATE-SECONDARY_boilerplate-0",
            ),
            forbidden_snippets=(
                "GLOBAL-NAVIGATION-NOISE_boilerplate-0_0",
                "GLOBAL-ASIDE-NOISE_boilerplate-0",
            ),
            expected_text=_boilerplate_expected_text("boilerplate-0"),
        ),
        BenchmarkCase(
            case_id="docs-0",
            path="/docs/docs-0",
            category="documentation",
            capability_tags=("static_html", "documentation_page", "code_block_retention"),
            required_snippets=(
                "GOLD-DOCS-PRIMARY_docs-0",
                "GOLD-DOCS-CODE_docs-0",
                "GOLD-DOCS-SECONDARY_docs-0",
            ),
            forbidden_snippets=("DOCS-SIDEBAR-NOISE_docs-0",),
            expected_text=_docs_expected_text("docs-0"),
        ),
        BenchmarkCase(
            case_id="product-0",
            path="/product/product-0",
            category="product_page",
            capability_tags=("static_html", "structured_product_page", "json_ld_page"),
            required_snippets=(
                "GOLD-PRODUCT-NAME_product-0",
                "GOLD-PRODUCT-PRICE_product-0",
                "GOLD-PRODUCT-DESCRIPTION_product-0",
            ),
            forbidden_snippets=("SHOP-NAV-NOISE_product-0",),
            expected_text=_product_expected_text("product-0"),
        ),
        BenchmarkCase(
            case_id="listing-0",
            path="/listing/listing-0",
            category="listing_page",
            capability_tags=("static_html", "multi_item_extraction", "listing_page"),
            required_snippets=(
                "GOLD-LISTING-ITEM-A_listing-0",
                "GOLD-LISTING-ITEM-B_listing-0",
                "GOLD-LISTING-ITEM-C_listing-0",
            ),
            forbidden_snippets=("LISTING-FILTER-NOISE_listing-0",),
            expected_text=_listing_expected_text("listing-0"),
        ),
        BenchmarkCase(
            case_id="forum-0",
            path="/forum/forum-0",
            category="discussion_thread",
            capability_tags=("static_html", "discussion_thread", "multi_item_extraction"),
            required_snippets=(
                "GOLD-FORUM-QUESTION_forum-0",
                "GOLD-FORUM-ANSWER_forum-0",
                "GOLD-FORUM-FOLLOWUP_forum-0",
            ),
            forbidden_snippets=("FORUM-HEADER-NOISE_forum-0",),
            expected_text=_forum_expected_text("forum-0"),
        ),
        BenchmarkCase(
            case_id="table-0",
            path="/table/table-0",
            category="data_table",
            capability_tags=("static_html", "table_extraction", "structured_content"),
            required_snippets=(
                "GOLD-TABLE-HEADER_table-0",
                "GOLD-TABLE-ROW-A_table-0",
                "GOLD-TABLE-ROW-B_table-0",
            ),
            forbidden_snippets=(),
            expected_text=_table_expected_text("table-0"),
        ),
        BenchmarkCase(
            case_id="slow-0",
            path="/slow/slow-0?delay=0.15",
            category="slow_static",
            capability_tags=("static_html", "latency_tolerance"),
            required_snippets=("GOLD-SLOW-PRIMARY_slow-0", "GOLD-SLOW-SECONDARY_slow-0"),
            forbidden_snippets=(),
            expected_text=_slow_expected_text("slow-0"),
        ),
        BenchmarkCase(
            case_id="redirect-0",
            path="/redirect/redirect-0",
            category="redirect_static",
            capability_tags=("static_html", "redirect_handling", "article_extraction"),
            required_snippets=(
                "GOLD-STATIC-PRIMARY_redirect-0",
                "GOLD-STATIC-SECONDARY_redirect-0",
                "GOLD-STATIC-TERTIARY_redirect-0",
            ),
            forbidden_snippets=(
                "GLOBAL-NAVIGATION-NOISE_redirect-0",
                "GLOBAL-FOOTER-NOISE_redirect-0",
            ),
            expected_text=_article_expected_text("redirect-0"),
        ),
        BenchmarkCase(
            case_id="shell-0",
            path="/shell/shell-0",
            category="client_rendered_shell",
            capability_tags=("javascript_required", "shell_detection"),
            required_snippets=("GOLD-JS-PRIMARY_shell-0", "GOLD-JS-SECONDARY_shell-0"),
            forbidden_snippets=("CLIENT-SHELL-LOADING_shell-0", "NO-JS-FALLBACK-NOISE_shell-0"),
            expected_text=_shell_expected_text("shell-0"),
        ),
        BenchmarkCase(
            case_id="shell-1",
            path="/shell/shell-1",
            category="client_rendered_shell",
            capability_tags=("javascript_required", "shell_detection"),
            required_snippets=("GOLD-JS-PRIMARY_shell-1", "GOLD-JS-SECONDARY_shell-1"),
            forbidden_snippets=("CLIENT-SHELL-LOADING_shell-1", "NO-JS-FALLBACK-NOISE_shell-1"),
            expected_text=_shell_expected_text("shell-1"),
        ),
    ]
    return cases


def _real_world_cases() -> list[BenchmarkCase]:
    """Representative public pages for opt-in real-world crawler benchmarking."""
    return [
        BenchmarkCase(
            case_id="books-to-scrape",
            path="https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
            category="demo_ecommerce",
            capability_tags=("static_html", "structured_product_page"),
            required_snippets=("A Light in the Attic",),
            forbidden_snippets=(),
        ),
        BenchmarkCase(
            case_id="quotes-to-scrape-static",
            path="https://quotes.toscrape.com/",
            category="demo_static_quotes",
            capability_tags=("static_html", "article_extraction"),
            required_snippets=("Albert Einstein",),
            forbidden_snippets=(),
        ),
        BenchmarkCase(
            case_id="quotes-to-scrape-js",
            path="https://quotes.toscrape.com/js/",
            category="demo_client_rendered",
            capability_tags=("javascript_required", "shell_detection"),
            required_snippets=("Albert Einstein",),
            forbidden_snippets=(),
        ),
        BenchmarkCase(
            case_id="python-docs",
            path="https://docs.python.org/3/tutorial/index.html",
            category="official_docs",
            capability_tags=("static_html", "documentation_page"),
            required_snippets=("The Python Tutorial", "Whetting Your Appetite"),
            forbidden_snippets=(),
        ),
        BenchmarkCase(
            case_id="mdn-javascript",
            path="https://developer.mozilla.org/en-US/docs/Web/JavaScript",
            category="developer_docs",
            capability_tags=("documentation_page", "modern_web_page"),
            required_snippets=("JavaScript", "MDN"),
            forbidden_snippets=(),
        ),
        BenchmarkCase(
            case_id="wikipedia-web-crawler",
            path="https://en.wikipedia.org/wiki/Web_crawler",
            category="encyclopedia",
            capability_tags=("static_html", "long_article"),
            required_snippets=("Web crawler", "search engine"),
            forbidden_snippets=(),
        ),
        BenchmarkCase(
            case_id="arxiv-abs",
            path="https://arxiv.org/abs/1706.03762",
            category="academic_abstract",
            capability_tags=("static_html", "academic_page"),
            required_snippets=("Attention Is All You Need",),
            forbidden_snippets=(),
        ),
    ]


@asynccontextmanager
async def benchmark_site(cases: list[BenchmarkCase]):
    """Run a local fixture site for deterministic crawler benchmarks."""

    case_by_id = {benchmark_case.case_id: benchmark_case for benchmark_case in cases}

    async def article(request: web.Request) -> web.Response:
        case_id = request.match_info["id"]
        return web.Response(text=_article_html(case_id), content_type="text/html")

    async def boilerplate(request: web.Request) -> web.Response:
        case_id = request.match_info["id"]
        return web.Response(text=_boilerplate_html(case_id), content_type="text/html")

    async def docs(request: web.Request) -> web.Response:
        case_id = request.match_info["id"]
        return web.Response(text=_docs_html(case_id), content_type="text/html")

    async def product(request: web.Request) -> web.Response:
        case_id = request.match_info["id"]
        return web.Response(text=_product_html(case_id), content_type="text/html")

    async def listing(request: web.Request) -> web.Response:
        case_id = request.match_info["id"]
        return web.Response(text=_listing_html(case_id), content_type="text/html")

    async def forum(request: web.Request) -> web.Response:
        case_id = request.match_info["id"]
        return web.Response(text=_forum_html(case_id), content_type="text/html")

    async def table(request: web.Request) -> web.Response:
        case_id = request.match_info["id"]
        return web.Response(text=_table_html(case_id), content_type="text/html")

    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(float(request.query.get("delay", "0.15")))
        return web.Response(text=_slow_html(request.match_info["id"]), content_type="text/html")

    async def redirect(request: web.Request) -> web.Response:
        case_id = request.match_info["id"]
        raise web.HTTPFound(location=f"/article/{case_id}")

    async def shell(request: web.Request) -> web.Response:
        return web.Response(text=_shell_html(request.match_info["id"]), content_type="text/html")

    async def reader(request: web.Request) -> web.Response:
        target_url = unquote(request.match_info["target"])
        benchmark_case = next(
            (
                item
                for item in case_by_id.values()
                if item.path.split("?")[0] in target_url or item.case_id in target_url
            ),
            None,
        )
        if not benchmark_case:
            return web.Response(status=404, text="unknown reader target")

        text = "\n\n".join(
            [
                f"# Reader fixture {benchmark_case.case_id}",
                benchmark_case.expected_text or "\n".join(benchmark_case.required_snippets),
                "Reader fixture body contains deterministic extracted content for fair "
                "fallback benchmarking, with enough stable text to pass minimum length checks.",
            ]
        )
        return web.Response(text=text, content_type="text/markdown")

    app = web.Application()
    app.router.add_get("/article/{id}", article)
    app.router.add_get("/boilerplate/{id}", boilerplate)
    app.router.add_get("/docs/{id}", docs)
    app.router.add_get("/product/{id}", product)
    app.router.add_get("/listing/{id}", listing)
    app.router.add_get("/forum/{id}", forum)
    app.router.add_get("/table/{id}", table)
    app.router.add_get("/slow/{id}", slow)
    app.router.add_get("/redirect/{id}", redirect)
    app.router.add_get("/shell/{id}", shell)
    app.router.add_get("/reader/{target:.*}", reader)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    sockets = site._server.sockets if site._server else []
    port = sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


async def _run_rounds(
    profile: BenchmarkProfile,
    cases_by_url: dict[str, BenchmarkCase],
    rounds: int,
) -> dict[str, Any]:
    urls = list(cases_by_url)
    samples: list[dict[str, Any]] = []

    for round_index in range(rounds):
        started = time.perf_counter()
        result = await profile.runner(urls)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        quality = _evaluate_results(cases_by_url, result.get("results", []))
        samples.append(
            {
                "round": round_index + 1,
                "elapsed_ms": elapsed_ms,
                "success_count": result.get("success_count", 0),
                "failed_count": len(result.get("failed_urls", [])),
                "stage_hits": result.get("stage_hits", {}),
                "timings_ms": result.get("timings_ms", {}),
                "quality": quality,
            }
        )

    elapsed_values = [sample["elapsed_ms"] for sample in samples]
    usable_rates = [sample["quality"]["usable_rate"] for sample in samples]
    success_rates = [sample["quality"]["success_rate"] for sample in samples]
    recall_values = [sample["quality"]["mean_required_recall"] for sample in samples]
    noise_values = [sample["quality"]["mean_noise_penalty"] for sample in samples]
    text_precision_values = [sample["quality"]["mean_text_precision"] for sample in samples]
    text_f1_values = [sample["quality"]["mean_text_f1"] for sample in samples]
    successful_rounds = sum(1 for sample in samples if sample["success_count"] > 0)

    median_ms = round(statistics.median(elapsed_values), 2)
    p95_ms = round(_percentile(elapsed_values, 0.95), 2)
    mean_ms = round(statistics.mean(elapsed_values), 2)
    usable_rate = round(statistics.mean(usable_rates), 4)
    success_rate = round(statistics.mean(success_rates), 4)
    capability_summary = _merge_capability_stats(
        [sample["quality"]["by_capability"] for sample in samples]
    )
    category_summary = _merge_capability_stats(
        [sample["quality"]["by_category"] for sample in samples]
    )

    return {
        "name": profile.name,
        "description": profile.description,
        "available": successful_rounds > 0,
        "rounds": rounds,
        "url_count": len(urls),
        "reliability": {
            "successful_rounds": successful_rounds,
            "failed_rounds": rounds - successful_rounds,
            "round_success_rate": round(successful_rounds / rounds, 4) if rounds else 0.0,
        },
        "performance": {
            "median_ms": median_ms,
            "mean_ms": mean_ms,
            "p95_ms": p95_ms,
            "min_ms": round(min(elapsed_values), 2),
            "max_ms": round(max(elapsed_values), 2),
            "jitter_ms": round(max(elapsed_values) - min(elapsed_values), 2),
            "throughput_urls_per_second": round(len(urls) / (median_ms / 1000), 2)
            if median_ms
            else 0.0,
        },
        "quality": {
            "success_rate": success_rate,
            "usable_rate": usable_rate,
            "mean_required_recall": round(statistics.mean(recall_values), 4),
            "mean_noise_penalty": round(statistics.mean(noise_values), 4),
            "mean_text_precision": round(statistics.mean(text_precision_values), 4),
            "mean_text_f1": round(statistics.mean(text_f1_values), 4),
        },
        "capability": {
            "by_category": category_summary,
            "by_tag": capability_summary,
        },
        "stage_hits": _merge_stage_hits(samples),
        "samples": samples,
    }


def _percentile(values: list[float], percentile: float) -> float:
    sorted_values = sorted(values)
    if not sorted_values:
        return 0.0
    index = (len(sorted_values) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = index - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _merge_capability_stats(items: list[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    aggregate: dict[str, Counter] = defaultdict(Counter)
    for item in items:
        for key, stats in item.items():
            aggregate[key]["total"] += stats["total"]
            aggregate[key]["found"] += stats["found"]
            aggregate[key]["usable"] += stats["usable"]
            aggregate[key]["recall_sum"] += (
                stats.get("mean_required_recall", 0.0) * stats["total"]
            )
            aggregate[key]["noise_sum"] += stats.get("mean_noise_penalty", 0.0) * stats["total"]
            aggregate[key]["text_precision_sum"] += (
                stats.get("mean_text_precision", 0.0) * stats["total"]
            )
            aggregate[key]["text_f1_sum"] += stats.get("mean_text_f1", 0.0) * stats["total"]
    return _summarize_counter_map(aggregate)


def _merge_stage_hits(samples: list[dict[str, Any]]) -> dict[str, float]:
    keys = set()
    for sample in samples:
        keys.update(sample["stage_hits"])
    return {
        key: round(statistics.mean(sample["stage_hits"].get(key, 0) for sample in samples), 2)
        for key in sorted(keys)
    }


def _write_benchmark_report(report: dict[str, Any], output: str = BENCHMARK_OUTPUT) -> None:
    output_path = Path(output)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def _http_extractor_runner(urls: list[str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        tasks = [
            fetch_with_http_extractor(
                url,
                client=client,
                semaphore=asyncio.Semaphore(20),
                timeout_seconds=5.0,
                min_content_length=MIN_CONTENT_LENGTH,
            )
            for url in urls
        ]
        raw_results = await asyncio.gather(*tasks)

    results = [result for result in raw_results if result]
    failed_urls = [url for url, result in zip(urls, raw_results) if not result]
    return {
        "results": results,
        "success_count": len(results),
        "failed_urls": failed_urls,
        "stage_hits": {"fast_http": len(results)},
    }


async def _reader_runner(base_url: str, urls: list[str]) -> dict[str, Any]:
    reader_urls = [build_reader_api_url(url, reader_url=f"{base_url}/reader") for url in urls]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
        tasks = [session.get(url) for url in reader_urls]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[dict[str, str]] = []
        failed_urls: list[str] = []
        for original_url, response in zip(urls, responses):
            if isinstance(response, Exception):
                failed_urls.append(original_url)
                continue
            async with response:
                body = await response.text()
                if response.status == 200 and _content_length(body) >= MIN_CONTENT_LENGTH:
                    results.append({"content": body, "reference": original_url})
                else:
                    failed_urls.append(original_url)

    return {
        "results": results,
        "success_count": len(results),
        "failed_urls": failed_urls,
        "stage_hits": {"reader": len(results)},
    }


def _scrapling_content_from_page(page: Any) -> str:
    if hasattr(page, "get_all_text"):
        return str(page.get_all_text(separator="\n", strip=True))
    if hasattr(page, "text"):
        text = page.text
        return text() if callable(text) else str(text)
    if hasattr(page, "body"):
        body = page.body
        if isinstance(body, bytes):
            return body.decode("utf-8", errors="replace")
        return str(body)
    return str(page)


async def _scrapling_static_runner(urls: list[str]) -> dict[str, Any]:
    try:
        from scrapling.fetchers import AsyncFetcher
    except Exception:
        return {"results": [], "success_count": 0, "failed_urls": urls, "stage_hits": {}}

    async def fetch_one(url: str) -> Optional[dict[str, str]]:
        try:
            page = await AsyncFetcher.get(
                url,
                stealthy_headers=True,
                follow_redirects=True,
                timeout=15,
            )
            status = getattr(page, "status", 200)
            content = _scrapling_content_from_page(page)
            if status and int(status) >= 400:
                return None
            if _content_length(content) < MIN_CONTENT_LENGTH:
                return None
            return {"content": content, "reference": url}
        except Exception:
            return None

    results = await asyncio.gather(*(fetch_one(url) for url in urls))
    successful = [result for result in results if result]
    failed_urls = [url for url, result in zip(urls, results) if not result]
    return {
        "results": successful,
        "success_count": len(successful),
        "failed_urls": failed_urls,
        "stage_hits": {"scrapling_static": len(successful)},
    }


async def _scrapling_dynamic_runner(urls: list[str]) -> dict[str, Any]:
    try:
        from scrapling.fetchers import DynamicFetcher
    except Exception:
        return {"results": [], "success_count": 0, "failed_urls": urls, "stage_hits": {}}

    semaphore = asyncio.Semaphore(2)

    async def fetch_one(url: str) -> Optional[dict[str, str]]:
        async with semaphore:
            try:
                page = await DynamicFetcher.async_fetch(
                    url,
                    headless=True,
                    disable_resources=True,
                    network_idle=True,
                    timeout=15000,
                )
                status = getattr(page, "status", 200)
                content = _scrapling_content_from_page(page)
                if status and int(status) >= 400:
                    return None
                if _content_length(content) < MIN_CONTENT_LENGTH:
                    return None
                return {"content": content, "reference": url}
            except Exception:
                return None

    results = await asyncio.gather(*(fetch_one(url) for url in urls))
    successful = [result for result in results if result]
    failed_urls = [url for url, result in zip(urls, results) if not result]
    return {
        "results": successful,
        "success_count": len(successful),
        "failed_urls": failed_urls,
        "stage_hits": {"scrapling_dynamic": len(successful)},
    }


async def _crawler_profile_runner(
    crawler: WebCrawler,
    urls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    http_enabled: bool,
    reader_enabled: bool,
    reader_url: Optional[str] = None,
) -> dict[str, Any]:
    import trailsearch.crawler as crawler_module
    import trailsearch.reader as reader_module

    monkeypatch.setattr(crawler_module, "HTTP_EXTRACTOR_ENABLED", http_enabled)
    monkeypatch.setattr(crawler_module, "READER_ENABLED", reader_enabled)
    monkeypatch.setattr(crawler_module, "HTTP_EXTRACTOR_MIN_CONTENT_LENGTH", MIN_CONTENT_LENGTH)
    monkeypatch.setattr(crawler_module, "READER_MIN_CONTENT_LENGTH", MIN_CONTENT_LENGTH)
    monkeypatch.setattr(crawler_module, "READER_TIMEOUT_SECONDS", 5.0)
    if reader_url:
        original_reader_url_builder = reader_module.build_reader_api_url

        def build_local_reader_url(url: str, reader_url: str = reader_url) -> str:
            return original_reader_url_builder(url, reader_url=reader_url)

        monkeypatch.setattr(reader_module, "build_reader_api_url", build_local_reader_url)
    try:
        result = await crawler.crawl_urls(urls, instruction="benchmark")
    except Exception:
        return {"results": [], "success_count": 0, "failed_urls": urls, "stage_hits": {}}

    return {
        "results": result.get("results", []),
        "success_count": result.get("success_count", 0),
        "failed_urls": result.get("failed_urls", []),
        "stage_hits": {
            "fast_http": result.get("fast_path_hits", 0),
            "reader": result.get("reader_hits", 0),
            "obscura": result.get("obscura_browser_hits", 0),
            "remote_browser": result.get("remote_browser_hits", 0),
            "local_browser": result.get("local_browser_fallback_hits", 0),
        },
        "timings_ms": result.get("timings_ms", {}),
    }


def _create_browser_crawler(
    anti_crawl_config: AntiCrawlConfig,
    backend_name: str,
    page_client: Optional[httpx.AsyncClient] = None,
) -> WebCrawler:
    crawler = WebCrawler(
        anti_crawl_config=anti_crawl_config,
        page_client=page_client,
        http_semaphore=asyncio.Semaphore(20) if page_client else None,
    )
    crawler.obscura_browser_backend.enabled = backend_name == "obscura"
    crawler.remote_browser_backend.enabled = backend_name == "remote"
    crawler.local_browser_backend.enabled = backend_name == "local"
    crawler.local_browser_backend.install_local_browser = BENCHMARK_INSTALL_BROWSERS
    return crawler


def _select_profiles(
    profiles: list[BenchmarkProfile],
    selected_names: set[str],
    env_var_name: str,
) -> list[BenchmarkProfile]:
    if not selected_names:
        return profiles

    selected = [profile for profile in profiles if profile.name in selected_names]
    if not selected:
        pytest.fail(f"{env_var_name} did not match any benchmark profiles")
    return selected


def _default_benchmark_profile_names() -> set[str]:
    presets = {
        "fast": {
            "http_extractor",
            "reader_service",
            "scrapling_static",
            "full_pipeline_reader",
        },
        "quick": {
            "http_extractor",
            "reader_service",
            "scrapling_static",
            "local_playwright",
            "full_pipeline_reader",
        },
        "browser": {
            "http_extractor",
            "scrapling_dynamic",
            "local_playwright",
            "full_pipeline",
            "full_pipeline_reader",
        },
        "all": set(),
    }
    if BENCHMARK_PRESET not in presets:
        pytest.fail(
            "TRAILSEARCH_BENCHMARK_PRESET must be one of: "
            f"{', '.join(sorted(presets))}"
        )
    return presets[BENCHMARK_PRESET]


def _best_dimension_rows(
    profiles: list[dict[str, Any]],
    dimension: str,
    label: str,
) -> list[dict[str, Any]]:
    rows = []
    for profile in profiles:
        if profile.get("name") in {"reader_service"}:
            continue
        stats = profile.get("capability", {}).get(dimension, {}).get(label)
        if not stats:
            continue
        rows.append(
            {
                "profile": profile["name"],
                "available": profile.get("available", False),
                "usable_rate": stats.get("usable_rate", 0.0),
                "success_rate": stats.get("success_rate", 0.0),
                "text_f1": stats.get("mean_text_f1", 0.0),
                "median_ms": profile.get("performance", {}).get("median_ms", 0.0),
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            not item["available"],
            -item["usable_rate"],
            -item["text_f1"],
            item["median_ms"],
        ),
    )


def _build_scenario_recommendations(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scenario_labels = [
        ("by_tag", "static_html", "mostly static HTML pages"),
        ("by_tag", "article_extraction", "articles and redirects"),
        ("by_tag", "boilerplate_removal", "navigation-heavy pages"),
        ("by_tag", "documentation_page", "developer documentation"),
        ("by_tag", "structured_product_page", "product detail pages"),
        ("by_tag", "multi_item_extraction", "listings and discussion threads"),
        ("by_tag", "javascript_required", "client-rendered shell pages"),
        ("by_tag", "latency_tolerance", "slow but valid static pages"),
    ]
    recommendations = []
    for dimension, label, use_when in scenario_labels:
        leaderboard = _best_dimension_rows(profiles, dimension, label)
        if not leaderboard:
            continue
        winner = leaderboard[0]
        recommendations.append(
            {
                "scenario": label,
                "use_when": use_when,
                "best_profile": winner["profile"],
                "best_usable_rate": winner["usable_rate"],
                "best_text_f1": winner["text_f1"],
                "best_median_ms": winner["median_ms"],
                "leaderboard": leaderboard[:5],
            }
        )
    return recommendations


def _build_recommendations(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recommendations = []
    for profile in profiles:
        performance = profile["performance"]
        quality = profile["quality"]
        capability = profile["capability"]["by_tag"]
        recommendations.append(
            {
                "name": profile["name"],
                "positioning": _position_profile(profile),
                "available": profile["available"],
                "usable_rate": quality["usable_rate"],
                "text_f1": quality.get("mean_text_f1", 0.0),
                "median_ms": performance["median_ms"],
                "throughput_urls_per_second": performance["throughput_urls_per_second"],
                "javascript_usable_rate": capability.get("javascript_required", {}).get(
                    "usable_rate", 0.0
                ),
                "boilerplate_usable_rate": capability.get("boilerplate_removal", {}).get(
                    "usable_rate", 0.0
                ),
            }
        )
    return sorted(
        recommendations,
        key=lambda item: (
            not item["available"],
            -item["usable_rate"],
            item["median_ms"],
        ),
    )


def _position_profile(profile: dict[str, Any]) -> str:
    name = profile["name"]
    usable_rate = profile["quality"]["usable_rate"]
    js_rate = profile["capability"]["by_tag"].get("javascript_required", {}).get(
        "usable_rate", 0.0
    )
    throughput = profile["performance"]["throughput_urls_per_second"]

    if not profile["available"]:
        return "unavailable_in_current_environment"
    if name in {"http_extractor", "full_pipeline"} and throughput >= 5 and usable_rate >= 0.5:
        return "primary_fast_path_candidate"
    if js_rate > 0:
        return "javascript_fallback_candidate"
    if usable_rate >= 0.8:
        return "high_quality_fallback_candidate"
    return "limited_or_specialized_candidate"


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_crawler_stage_benchmark(monkeypatch):
    """Benchmark crawler stages and write a JSON report for ranking extraction priorities."""
    if not RUN_BENCHMARK:
        pytest.skip("Set TRAILSEARCH_RUN_BENCHMARK=1 to run crawler benchmarks")

    anti_crawl_config = AntiCrawlConfig(
        enable_proxy_rotation=False,
        enable_user_agent_rotation=False,
        enable_request_delay=False,
        enable_random_headers=False,
        enable_browser_headers=False,
    )
    cases = _benchmark_cases()

    async with benchmark_site(cases) as base_url:
        cases_by_url = {f"{base_url}{benchmark_case.path}": benchmark_case for benchmark_case in cases}

        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as page_client:
            obscura_crawler = WebCrawler(
                anti_crawl_config=anti_crawl_config,
                page_client=None,
            )
            obscura_crawler.obscura_browser_backend.enabled = True
            obscura_crawler.obscura_browser_backend.allow_private_network = True
            obscura_crawler.remote_browser_backend.enabled = False
            obscura_crawler.local_browser_backend.enabled = False

            local_browser_crawler = WebCrawler(
                anti_crawl_config=anti_crawl_config,
                page_client=None,
            )
            local_browser_crawler.obscura_browser_backend.enabled = False
            local_browser_crawler.remote_browser_backend.enabled = False
            local_browser_crawler.local_browser_backend.enabled = True
            local_browser_crawler.local_browser_backend.install_local_browser = (
                BENCHMARK_INSTALL_BROWSERS
            )

            full_pipeline_crawler = WebCrawler(
                anti_crawl_config=anti_crawl_config,
                page_client=page_client,
                http_semaphore=asyncio.Semaphore(20),
            )
            full_pipeline_crawler.obscura_browser_backend.enabled = True
            full_pipeline_crawler.obscura_browser_backend.allow_private_network = True
            full_pipeline_crawler.remote_browser_backend.enabled = False
            full_pipeline_crawler.local_browser_backend.enabled = False

            full_pipeline_reader_crawler = WebCrawler(
                anti_crawl_config=anti_crawl_config,
                page_client=page_client,
                http_semaphore=asyncio.Semaphore(20),
                reader_semaphore=asyncio.Semaphore(20),
            )
            full_pipeline_reader_crawler.obscura_browser_backend.enabled = False
            full_pipeline_reader_crawler.remote_browser_backend.enabled = False
            full_pipeline_reader_crawler.local_browser_backend.enabled = True
            full_pipeline_reader_crawler.local_browser_backend.install_local_browser = (
                BENCHMARK_INSTALL_BROWSERS
            )

            profiles = [
                BenchmarkProfile(
                    name="http_extractor",
                    description="Static HTTP fetch + trafilatura extraction; best default fast path.",
                    runner=_http_extractor_runner,
                ),
                BenchmarkProfile(
                    name="reader_service",
                    description="Reader-like markdown service fallback using local fixture endpoint.",
                    runner=lambda urls: _reader_runner(base_url, urls),
                ),
                BenchmarkProfile(
                    name="scrapling_static",
                    description="Scrapling AsyncFetcher static extraction; optional dependency.",
                    runner=_scrapling_static_runner,
                ),
                BenchmarkProfile(
                    name="scrapling_dynamic",
                    description="Scrapling DynamicFetcher browser-backed extraction; optional dependency.",
                    runner=_scrapling_dynamic_runner,
                ),
                BenchmarkProfile(
                    name="obscura_cli",
                    description="Obscura CLI browser fallback; requires OBSCURA_BINARY on PATH.",
                    runner=lambda urls: _crawler_profile_runner(
                        obscura_crawler,
                        urls,
                        monkeypatch,
                        http_enabled=False,
                        reader_enabled=False,
                    ),
                ),
                BenchmarkProfile(
                    name="local_playwright",
                    description=(
                        "Crawl4AI local Playwright browser fallback; benchmark does not install "
                        "missing browsers unless TRAILSEARCH_BENCHMARK_INSTALL_BROWSERS=1."
                    ),
                    runner=lambda urls: _crawler_profile_runner(
                        local_browser_crawler,
                        urls,
                        monkeypatch,
                        http_enabled=False,
                        reader_enabled=False,
                    ),
                ),
                BenchmarkProfile(
                    name="full_pipeline",
                    description="HTTP fast path plus browser fallback, with Reader disabled.",
                    runner=lambda urls: _crawler_profile_runner(
                        full_pipeline_crawler,
                        urls,
                        monkeypatch,
                        http_enabled=True,
                        reader_enabled=False,
                    ),
                ),
                BenchmarkProfile(
                    name="full_pipeline_reader",
                    description=(
                        "HTTP fast path plus local Reader fallback, then browser only for remaining misses."
                    ),
                    runner=lambda urls: _crawler_profile_runner(
                        full_pipeline_reader_crawler,
                        urls,
                        monkeypatch,
                        http_enabled=True,
                        reader_enabled=True,
                        reader_url=f"{base_url}/reader",
                    ),
                ),
            ]
            selected_profile_names = BENCHMARK_PROFILES or _default_benchmark_profile_names()
            profiles = _select_profiles(
                profiles,
                selected_profile_names,
                "TRAILSEARCH_BENCHMARK_PROFILES",
            )

            profile_reports = [
                await _run_rounds(profile, cases_by_url, BENCHMARK_ROUNDS) for profile in profiles
            ]
            report = {
                "methodology": {
                    "principles": [
                        "Separate extraction quality from latency and throughput.",
                        "Use local deterministic fixtures to reduce external network variance.",
                        "Score required content recall and boilerplate/noise leakage.",
                        "Use token-level ground-truth F1 to separate clean extraction from noisy full-page text.",
                        "Break down capability by page category and required crawler feature.",
                        "Keep the default quick preset short; use all/browser presets only for deeper studies.",
                        "Exclude standalone stage probes from scenario winners so deployable pipelines are recommended.",
                        "Report unavailable tools instead of silently excluding them.",
                    ],
                    "metrics": {
                        "success_rate": "Returned any content for the fixture URL.",
                        "usable_rate": "Returned enough content, hit most required snippets, and avoided forbidden snippets.",
                        "mean_required_recall": "Average required gold snippet recall across fixtures.",
                        "mean_text_f1": "Token-overlap F1 between extracted text and fixture ground truth.",
                        "mean_noise_penalty": "Average forbidden boilerplate leakage ratio.",
                        "median_ms": "Median end-to-end profile runtime across benchmark rounds.",
                        "jitter_ms": "Max minus min runtime; useful for stability comparisons.",
                    },
                },
                "meta": {
                    "rounds": BENCHMARK_ROUNDS,
                    "url_count": len(cases_by_url),
                    "output": BENCHMARK_OUTPUT,
                    "preset": BENCHMARK_PRESET,
                    "browser_install_allowed": BENCHMARK_INSTALL_BROWSERS,
                    "profiles": [profile.name for profile in profiles],
                    "cases": [asdict(benchmark_case) for benchmark_case in cases],
                },
                "profiles": profile_reports,
                "recommendations": _build_recommendations(profile_reports),
                "scenario_recommendations": _build_scenario_recommendations(profile_reports),
            }

            _write_benchmark_report(report)
            print(json.dumps(report["recommendations"], ensure_ascii=False, indent=2))

            await obscura_crawler.close()
            await local_browser_crawler.close()
            await full_pipeline_crawler.close()
            await full_pipeline_reader_crawler.close()


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_real_world_crawler_benchmark(monkeypatch):
    """Benchmark representative public sites with weak-gold quality checks."""
    if not RUN_REAL_BENCHMARK:
        pytest.skip("Set TRAILSEARCH_RUN_REAL_BENCHMARK=1 to run real-world benchmarks")

    anti_crawl_config = AntiCrawlConfig(
        enable_proxy_rotation=False,
        enable_user_agent_rotation=False,
        enable_request_delay=False,
        enable_random_headers=False,
        enable_browser_headers=False,
    )
    cases = _real_world_cases()
    cases_by_url = {benchmark_case.path: benchmark_case for benchmark_case in cases}

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
        headers={"User-Agent": "TrailSearch-Benchmark/1.0"},
    ) as page_client:
        obscura_crawler = _create_browser_crawler(anti_crawl_config, "obscura")
        local_browser_crawler = _create_browser_crawler(anti_crawl_config, "local")
        full_pipeline_crawler = _create_browser_crawler(
            anti_crawl_config,
            "obscura",
            page_client=page_client,
        )

        profiles = [
            BenchmarkProfile(
                name="http_extractor",
                description="HTTP + trafilatura against representative public pages.",
                runner=_http_extractor_runner,
            ),
            BenchmarkProfile(
                name="scrapling_static",
                description="Scrapling AsyncFetcher against representative public pages.",
                runner=_scrapling_static_runner,
            ),
            BenchmarkProfile(
                name="scrapling_dynamic",
                description="Scrapling DynamicFetcher against representative public pages.",
                runner=_scrapling_dynamic_runner,
            ),
            BenchmarkProfile(
                name="obscura_cli",
                description="Obscura CLI browser fallback against representative public pages.",
                runner=lambda urls: _crawler_profile_runner(
                    obscura_crawler,
                    urls,
                    monkeypatch,
                    http_enabled=False,
                    reader_enabled=False,
                ),
            ),
            BenchmarkProfile(
                name="local_playwright",
                description="Crawl4AI local Playwright fallback against representative public pages.",
                runner=lambda urls: _crawler_profile_runner(
                    local_browser_crawler,
                    urls,
                    monkeypatch,
                    http_enabled=False,
                    reader_enabled=False,
                ),
            ),
            BenchmarkProfile(
                name="full_pipeline",
                description="Current staged crawler against representative public pages.",
                runner=lambda urls: _crawler_profile_runner(
                    full_pipeline_crawler,
                    urls,
                    monkeypatch,
                    http_enabled=True,
                    reader_enabled=False,
                ),
            ),
        ]
        profiles = _select_profiles(
            profiles,
            REAL_BENCHMARK_PROFILES,
            "TRAILSEARCH_REAL_BENCHMARK_PROFILES",
        )

        profile_reports = [
            await _run_rounds(profile, cases_by_url, REAL_BENCHMARK_ROUNDS)
            for profile in profiles
        ]
        report = {
            "methodology": {
                "real_world": True,
                "caveats": [
                    "Real websites can change content, block requests, or vary by region/time.",
                    "Scores use weak-gold keyword checks rather than exact article ground truth.",
                    "Use local fixture benchmarks for regression tests and real-world benchmarks for external validity.",
                    "Keep rounds and profile count modest to avoid unnecessary load on public sites.",
                ],
                "metrics": {
                    "success_rate": "Returned any content for the live URL.",
                    "usable_rate": "Returned enough content and matched expected stable keywords.",
                    "mean_text_f1": "Weak-gold token overlap against expected stable text.",
                    "by_category": "Separates docs, demo scraping pages, encyclopedia, academic pages, and JS-heavy pages.",
                },
            },
            "meta": {
                "rounds": REAL_BENCHMARK_ROUNDS,
                "url_count": len(cases_by_url),
                "output": REAL_BENCHMARK_OUTPUT,
                "browser_install_allowed": BENCHMARK_INSTALL_BROWSERS,
                "profiles": [profile.name for profile in profiles],
                "cases": [asdict(benchmark_case) for benchmark_case in cases],
            },
            "profiles": profile_reports,
            "recommendations": _build_recommendations(profile_reports),
            "scenario_recommendations": _build_scenario_recommendations(profile_reports),
        }

        _write_benchmark_report(report, output=REAL_BENCHMARK_OUTPUT)
        print(json.dumps(report["recommendations"], ensure_ascii=False, indent=2))

        await obscura_crawler.close()
        await local_browser_crawler.close()
        await full_pipeline_crawler.close()
