"""Push metrics straight to Grafana Cloud's Prometheus.

Grafana Cloud cannot scrape this process — there is no route from their network
to a Cloud Run container, and no route to a laptop either. The usual answer is
to run a Prometheus alongside and let it forward, but that means the graphs go
blank whenever that machine is off. Pushing from the app removes the third
moving part entirely.

The remote-write wire format is a snappy-compressed protobuf `WriteRequest`.
That is four small messages, so they are encoded by hand rather than pulling in
protobuf and a generated stub:

    WriteRequest { repeated TimeSeries timeseries = 1; }
    TimeSeries   { repeated Label labels = 1; repeated Sample samples = 2; }
    Label        { string name = 1; string value = 2; }
    Sample       { double value = 1; int64 timestamp = 2; }

Snappy comes from cramjam, which ships wheels and needs no system library.
"""
from __future__ import annotations

import logging
import struct
import time
from typing import Iterable

import httpx

log = logging.getLogger("gallery.remote_write")


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _delimited(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + _varint(len(payload)) + payload


def _label(name: str, value: str) -> bytes:
    return (_delimited(0x0A, name.encode()) +      # Label.name
            _delimited(0x12, value.encode()))       # Label.value


def _sample(value: float, ts_ms: int) -> bytes:
    return (b"\x09" + struct.pack("<d", value) +    # Sample.value, fixed64
            b"\x10" + _varint(ts_ms))               # Sample.timestamp, varint


def encode(series: Iterable[tuple[dict[str, str], float, int]]) -> bytes:
    """Build a WriteRequest from (labels, value, timestamp_ms) triples."""
    body = bytearray()
    for labels, value, ts in series:
        ts_body = bytearray()
        # Prometheus requires labels sorted by name, __name__ included.
        for k in sorted(labels):
            ts_body += _delimited(0x0A, _label(k, labels[k]))   # TimeSeries.labels
        ts_body += _delimited(0x12, _sample(value, ts))          # TimeSeries.samples
        body += _delimited(0x0A, bytes(ts_body))                 # WriteRequest.timeseries
    return bytes(body)


class RemoteWriter:
    def __init__(self, url: str, username: str, token: str,
                 extra_labels: dict[str, str] | None = None) -> None:
        self.url = url
        self.enabled = bool(url and username and token)
        self.extra = extra_labels or {}
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None
        self._client = httpx.AsyncClient(
            timeout=10.0,
            auth=(username, token) if self.enabled else None,
            headers={
                "Content-Type": "application/x-protobuf",
                "Content-Encoding": "snappy",
                "X-Prometheus-Remote-Write-Version": "0.1.0",
                "User-Agent": "the-gallery/1.0",
            },
        )

    async def push(self, samples: Iterable[tuple[str, dict[str, str], float]]) -> bool:
        """Send (metric_name, labels, value) triples, stamped now."""
        if not self.enabled:
            return False
        ts_ms = int(time.time() * 1000)
        series = []
        for name, labels, value in samples:
            merged = {"__name__": name, **self.extra, **labels}
            series.append((merged, float(value), ts_ms))
        if not series:
            return False

        try:
            import cramjam
            payload = bytes(cramjam.snappy.compress_raw(encode(series)))
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"encode: {exc}"
            log.warning("remote_write encode failed: %s", exc)
            return False

        try:
            r = await self._client.post(self.url, content=payload)
            if r.status_code < 300:
                self.sent += len(series)
                self.last_error = None
                return True
            self.failed += 1
            self.last_error = f"{r.status_code} {r.text[:140]}"
            log.warning("remote_write %s", self.last_error)
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            self.last_error = str(exc)[:140]
            log.warning("remote_write failed: %s", self.last_error)
        return False

    async def aclose(self) -> None:
        await self._client.aclose()
