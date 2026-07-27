"""
Tests for config module
"""

from trailsearch.config import get_config_info


def test_get_config_info():
    """Test that get_config_info returns expected structure"""
    config = get_config_info()

    assert isinstance(config, dict)
    assert "searxng" in config
    assert "api" in config
    assert "crawler" in config
    assert "browser" in config
    assert "cache" in config
    assert "reader" in config
    assert "search_engines" in config
    assert "search_provider" in config
    assert "local_index" in config
    assert "backfill" in config
    assert "service_registry" in config

    # Check SearXNG config structure
    assert "host" in config["searxng"]
    assert "port" in config["searxng"]
    assert "base_path" in config["searxng"]
    assert "api_base" in config["searxng"]

    # Check API config structure
    assert "host" in config["api"]
    assert "port" in config["api"]

    # Check crawler config structure
    assert "default_search_limit" in config["crawler"]
    assert "content_filter_threshold" in config["crawler"]
    assert "word_count_threshold" in config["crawler"]
    assert "pool_size" in config["crawler"]
    assert "searxng_timeout_seconds" in config["crawler"]
    assert "http_extractor_enabled" in config["crawler"]
    assert "http_extractor_timeout_seconds" in config["crawler"]
    assert "http_extractor_max_concurrency" in config["crawler"]
    assert "http_extractor_min_content_length" in config["crawler"]
    assert "extraction_strategy" in config["crawler"]
    assert "quality_gate_enabled" in config["crawler"]
    assert "min_quality_score" in config["crawler"]

    # Check browser config structure
    assert "backend" in config["browser"]
    assert "browserless_ws_url" in config["browser"]
    assert "remote_timeout_seconds" in config["browser"]
    assert "remote_max_concurrency" in config["browser"]
    assert "local_fallback_enabled" in config["browser"]
    assert "local_max_concurrency" in config["browser"]
    assert "obscura_binary" in config["browser"]
    assert "obscura_timeout_seconds" in config["browser"]
    assert "obscura_max_concurrency" in config["browser"]
    assert "obscura_stealth_enabled" in config["browser"]
    assert "obscura_wait_until" in config["browser"]
    assert "obscura_dump_format" in config["browser"]
    assert "obscura_allow_private_network" in config["browser"]

    # Check cache config structure
    assert "enabled" in config["cache"]
    assert "redis_url" in config["cache"]
    assert "crawl_ttl_hours" in config["cache"]
    assert "search_ttl_seconds" in config["cache"]

    # Check reader config structure
    assert "enabled" in config["reader"]
    assert "url" in config["reader"]
    assert "urls" in config["reader"]
    assert "timeout_seconds" in config["reader"]
    assert "max_concurrency" in config["reader"]
    assert "min_content_length" in config["reader"]

    # Check search engines config structure
    assert "disabled" in config["search_engines"]
    assert "enabled" in config["search_engines"]
    assert "default" in config["search_provider"]
    assert "route_providers" in config["search_provider"]
    assert "enabled" in config["local_index"]
    assert "path" in config["local_index"]
    assert "enabled" in config["backfill"]
    assert "queue_backend" in config["backfill"]
    assert "max_attempts" in config["backfill"]
    assert "etcd_enabled" in config["service_registry"]
    assert "etcd_endpoints" in config["service_registry"]
    assert "etcd_self_services" in config["service_registry"]


def test_config_types():
    """Test that config values have correct types"""
    config = get_config_info()

    assert isinstance(config["searxng"]["host"], str)
    assert isinstance(config["searxng"]["port"], int)
    assert isinstance(config["api"]["port"], int)
    assert isinstance(config["crawler"]["default_search_limit"], int)
    assert isinstance(config["crawler"]["content_filter_threshold"], float)
    assert isinstance(config["crawler"]["pool_size"], int)
    assert isinstance(config["crawler"]["searxng_timeout_seconds"], float)
    assert isinstance(config["crawler"]["http_extractor_enabled"], bool)
    assert isinstance(config["crawler"]["http_extractor_timeout_seconds"], float)
    assert isinstance(config["crawler"]["http_extractor_max_concurrency"], int)
    assert isinstance(config["crawler"]["http_extractor_min_content_length"], int)
    assert isinstance(config["crawler"]["extraction_strategy"], str)
    assert isinstance(config["crawler"]["quality_gate_enabled"], bool)
    assert isinstance(config["crawler"]["min_quality_score"], float)
    assert isinstance(config["cache"]["search_ttl_seconds"], int)
    assert isinstance(config["browser"]["backend"], str)
    assert isinstance(config["browser"]["remote_timeout_seconds"], float)
    assert isinstance(config["browser"]["remote_max_concurrency"], int)
    assert isinstance(config["browser"]["local_fallback_enabled"], bool)
    assert isinstance(config["browser"]["local_max_concurrency"], int)
    assert isinstance(config["browser"]["obscura_binary"], str)
    assert isinstance(config["browser"]["obscura_timeout_seconds"], float)
    assert isinstance(config["browser"]["obscura_max_concurrency"], int)
    assert isinstance(config["browser"]["obscura_stealth_enabled"], bool)
    assert isinstance(config["browser"]["obscura_wait_until"], str)
    assert isinstance(config["browser"]["obscura_dump_format"], str)
    assert isinstance(config["browser"]["obscura_allow_private_network"], bool)
    assert isinstance(config["reader"]["timeout_seconds"], float)
    assert isinstance(config["reader"]["urls"], str)
    assert isinstance(config["reader"]["max_concurrency"], int)
    assert isinstance(config["reader"]["min_content_length"], int)
    assert isinstance(config["search_provider"]["default"], str)
    assert isinstance(config["search_provider"]["external_search_enabled"], bool)
    assert isinstance(config["local_index"]["enabled"], bool)
    assert isinstance(config["local_index"]["path"], str)
    assert isinstance(config["backfill"]["enabled"], bool)
    assert isinstance(config["backfill"]["queue_backend"], str)
    assert isinstance(config["backfill"]["batch_size"], int)
    assert isinstance(config["backfill"]["max_attempts"], int)
    assert isinstance(config["service_registry"]["etcd_enabled"], bool)
    assert isinstance(config["service_registry"]["etcd_endpoints"], str)
    assert isinstance(config["service_registry"]["etcd_self_services"], str)
