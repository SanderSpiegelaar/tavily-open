"""
Optional etcd-backed service registry and discovery.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger


@dataclass(frozen=True)
class ServiceInstance:
    """One discovered service instance."""

    service: str
    node_id: str
    endpoint: str
    metadata: dict[str, Any]


def default_node_id(prefix: str = "node") -> str:
    """Build a stable-enough node id for container deployments."""
    return f"{prefix}-{socket.gethostname()}-{os.getpid()}"


def default_node_endpoint(port: int, scheme: str = "http") -> str:
    """Build a container-reachable endpoint from the current hostname."""
    return f"{scheme}://{socket.gethostname()}:{port}"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _unb64(value: str) -> str:
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def _prefix_range_end(prefix: str) -> str:
    raw = bytearray(prefix.encode("utf-8"))
    if not raw:
        return "\0"
    raw[-1] += 1
    return raw.decode("utf-8", errors="ignore")


class EtcdServiceRegistry:
    """Small etcd v3 HTTP registry client."""

    def __init__(
        self,
        endpoints: str,
        namespace: str = "trailsearch",
        ttl_seconds: int = 30,
        refresh_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoints = [endpoint.strip().rstrip("/") for endpoint in endpoints.split(",") if endpoint.strip()]
        self.namespace = namespace.strip("/ ") or "trailsearch"
        self.ttl_seconds = ttl_seconds
        self.refresh_seconds = refresh_seconds
        self.client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task] = []
        self._endpoint_index = 0

    async def close(self) -> None:
        """Stop background registration and close owned HTTP resources."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    def start_registration(
        self,
        service: str,
        node_id: str,
        endpoint: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Start periodic lease-backed registration."""
        task = asyncio.create_task(
            self._registration_loop(service, node_id, endpoint, metadata or {}),
            name=f"trailsearch-registry-{service}-{node_id}",
        )
        self._tasks.append(task)

    async def discover(self, service: str) -> list[ServiceInstance]:
        """Discover currently registered instances for a service."""
        prefix = self._service_prefix(service)
        payload = {
            "key": _b64(prefix),
            "range_end": _b64(_prefix_range_end(prefix)),
        }
        try:
            response = await self._post("/v3/kv/range", payload)
            instances = []
            for item in response.get("kvs", []):
                key = _unb64(item.get("key", ""))
                value = json.loads(_unb64(item.get("value", "")))
                node_id = key.rsplit("/", 1)[-1]
                endpoint = value.get("endpoint", "")
                if endpoint:
                    instances.append(
                        ServiceInstance(
                            service=service,
                            node_id=node_id,
                            endpoint=endpoint,
                            metadata=value.get("metadata", {}),
                        )
                    )
            return instances
        except Exception as exc:
            logger.warning(f"etcd discovery failed for service '{service}': {exc}")
            return []

    async def register_once(
        self,
        service: str,
        node_id: str,
        endpoint: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a service once with a fresh lease."""
        lease = await self._grant_lease()
        value = {
            "service": service,
            "node_id": node_id,
            "endpoint": endpoint,
            "metadata": metadata or {},
            "updated_at": time.time(),
        }
        payload = {
            "key": _b64(self._service_key(service, node_id)),
            "value": _b64(json.dumps(value, ensure_ascii=False, sort_keys=True)),
            "lease": lease,
        }
        await self._post("/v3/kv/put", payload)

    async def _registration_loop(
        self,
        service: str,
        node_id: str,
        endpoint: str,
        metadata: dict[str, Any],
    ) -> None:
        while True:
            try:
                await self.register_once(service, node_id, endpoint, metadata)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"etcd registration failed for {service}/{node_id}: {exc}")
            await asyncio.sleep(self.refresh_seconds)

    async def _grant_lease(self) -> str:
        response = await self._post("/v3/lease/grant", {"TTL": self.ttl_seconds})
        lease = response.get("ID") or response.get("id")
        if lease is None:
            raise RuntimeError("etcd lease grant response did not include ID")
        return str(lease)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.endpoints:
            raise RuntimeError("No etcd endpoints configured")

        client = self.client or self._get_owned_client()
        last_error: Exception | None = None
        for _ in range(len(self.endpoints)):
            base_url = self.endpoints[self._endpoint_index % len(self.endpoints)]
            self._endpoint_index += 1
            try:
                response = await client.post(f"{base_url}{path}", json=payload)
                response.raise_for_status()
                return response.json() if response.content else {}
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"All etcd endpoints failed for {path}: {last_error}")

    def _get_owned_client(self) -> httpx.AsyncClient:
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        return self._owned_client

    def _service_prefix(self, service: str) -> str:
        return f"/{self.namespace}/services/{service}/"

    def _service_key(self, service: str, node_id: str) -> str:
        return f"{self._service_prefix(service)}{node_id}"
