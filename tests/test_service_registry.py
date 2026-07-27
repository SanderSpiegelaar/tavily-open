"""
Tests for optional etcd service registry support.
"""

import base64
import json

import httpx
import pytest

from trailsearch.service_registry import EtcdServiceRegistry, default_node_endpoint


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _unb64(value: str) -> str:
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


@pytest.mark.asyncio
async def test_discover_decodes_etcd_instances():
    """Discovery should decode etcd's base64 key/value response."""

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v3/kv/range"
        assert _unb64(payload["key"]) == "/test/services/reader/"
        return httpx.Response(
            200,
            json={
                "kvs": [
                    {
                        "key": _b64("/test/services/reader/reader-1"),
                        "value": _b64(
                            json.dumps(
                                {
                                    "endpoint": "http://reader-1:3000",
                                    "metadata": {"zone": "a"},
                                }
                            )
                        ),
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = EtcdServiceRegistry("http://etcd:2379", namespace="test", client=client)
    try:
        instances = await registry.discover("reader")
    finally:
        await client.aclose()

    assert len(instances) == 1
    assert instances[0].service == "reader"
    assert instances[0].node_id == "reader-1"
    assert instances[0].endpoint == "http://reader-1:3000"
    assert instances[0].metadata == {"zone": "a"}


@pytest.mark.asyncio
async def test_register_once_grants_lease_and_puts_service_key():
    """Registration should create a lease-backed etcd key."""
    requests: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/v3/lease/grant":
            return httpx.Response(200, json={"ID": "12345"})
        if request.url.path == "/v3/kv/put":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = EtcdServiceRegistry(
        "http://etcd:2379",
        namespace="test",
        ttl_seconds=15,
        client=client,
    )
    try:
        await registry.register_once(
            service="crawler",
            node_id="crawler-1",
            endpoint="http://crawler-1:3000",
            metadata={"role": "worker"},
        )
    finally:
        await client.aclose()

    assert requests[0] == ("/v3/lease/grant", {"TTL": 15})
    put_path, put_payload = requests[1]
    assert put_path == "/v3/kv/put"
    assert _unb64(put_payload["key"]) == "/test/services/crawler/crawler-1"
    assert put_payload["lease"] == "12345"

    value = json.loads(_unb64(put_payload["value"]))
    assert value["service"] == "crawler"
    assert value["node_id"] == "crawler-1"
    assert value["endpoint"] == "http://crawler-1:3000"
    assert value["metadata"] == {"role": "worker"}


def test_default_node_endpoint_uses_container_hostname():
    """Default endpoint should include the chosen port."""
    endpoint = default_node_endpoint(3000)

    assert endpoint.startswith("http://")
    assert endpoint.endswith(":3000")
