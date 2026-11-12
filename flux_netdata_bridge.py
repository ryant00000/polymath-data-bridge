#!/usr/bin/env python3
"""Flux Netdata Bridge.

Polls a Netdata agent, a llama.cpp server, and a Home Assistant instance and
mirrors the metrics onto Flux Polymath generic graph tiles through Polymath's
local HTTPS API.

The bridge runs on the machine that has the Flux keyboard attached.  Netdata
can live on another machine; only outbound HTTP(S) to Netdata and localhost
HTTPS to Polymath are needed.

Standard library only.  Tested against Netdata v2.12.x and the Polymath
1.4.5.x API.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

APP_NAME = "Flux Netdata Bridge"
VERSION = "1.0"
LOG = logging.getLogger("flux-netdata-bridge")

DEFAULT_CONFIG_PATH = "~/.config/flux-netdata-bridge/config.json"
DEFAULT_KEYS_JSON = "~/flux/config/keys.json"
DEFAULT_API_KEY_FILE = "~/.config/flux-netdata-bridge/api_key"
DEFAULT_STATE_FILE = "~/.local/state/flux-netdata-bridge/state.json"

# API graph tile types, confirmed against Polymath 1.4.5.x.
TILE_TYPE_GRAPH = 1

DEFAULTS: dict[str, Any] = {
    "netdata_url": "http://127.0.0.1:19999",
    "server_name": "",
    "poll_seconds": 2.0,
    "graph_length": 120,
    "time_unit": None,
    "keys_json": DEFAULT_KEYS_JSON,
    "api_key_file": DEFAULT_API_KEY_FILE,
    "state_file": DEFAULT_STATE_FILE,
    "user_agent": APP_NAME,
    "netdata_timeout": 5.0,
    "netdata_insecure": False,
    "netdata_ca": None,
    "llama_url": "http://127.0.0.1:10000",
    "llama_api_key": "imanapikey",
    "llama_timeout": 5.0,
    "llama_model_ttl": 0.0,
    "ha_url": "http://127.0.0.1:8123",
    "ha_token": "",
    "ha_timeout": 5.0,
    "tiles": [],
}

KNOWN_METRICS = {"cpu", "ram", "gpu", "llama", "disk", "net", "temp", "custom", "hass"}


class BridgeError(Exception):
    """Base class for expected bridge failures."""


class ConfigError(BridgeError):
    pass


class NetdataError(BridgeError):
    pass


class LlamaError(NetdataError):
    pass


class HassError(NetdataError):
    pass


class PolymathError(BridgeError):
    pass


class PolymathNotReady(PolymathError):
    pass


class ApiError(PolymathError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Polymath HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class ApiAuthError(ApiError):
    pass


class TileMissing(PolymathError):
    pass


def expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def compact(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_gib(gib: float) -> str:
    if gib >= 1024:
        return f"{gib / 1024:.1f} TiB"
    return f"{gib:.0f} GiB"


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def health_colour(value: float, warn: float, crit: float) -> str:
    if value >= crit:
        return "Red"
    if value >= warn:
        return "Yellow"
    return "Green"


@dataclass
class TileConfig:
    index: int
    title: str
    metric: str
    chart: str | None = None
    dimension: str | None = None
    entity: str | None = None
    attribute: str | None = None
    label: str | None = None
    unit: str | None = None
    scale: float = 1.0
    secondary: str | None = None
    warn: float | None = None
    crit: float | None = None
    # Per-tile timing, resolved against the global defaults in load_config().
    poll_seconds: float = 0.0
    graph_length: int = 0
    time_unit: str | None = None
    graph_length_explicit: bool = False
    # Runtime state, not from JSON.
    id: str = ""
    last_fields: dict[str, Any] | None = None
    last_ok: float | None = None
    last_error: str | None = None
    applied_graph_length: int | None = None
    next_poll: float = 0.0


def derive_time_unit(poll_seconds: float) -> str:
    """The x-axis label for a tile polled every ``poll_seconds`` seconds."""
    return "s" if poll_seconds == 1 else f"× {poll_seconds:g} s"


@dataclass
class ServerInfo:
    name: str = ""
    cores: int | None = None


def _require_number(raw: dict[str, Any], key: str, default: float | int) -> float:
    value = raw.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key!r} must be a number, got {value!r}") from None


def _tile_number(item: dict[str, Any], key: str, position: int, default: float | int) -> float:
    value = item.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"tiles[{position}].{key} must be a number, got {value!r}") from None


def _strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments outside JSON strings."""
    out: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    escaped = False
    while i < length:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < length and text[i + 1] == "/":
            i += 2
            while i < length and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < length and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated block comment")
            block = text[i : end + 2]
            out.append(" ")
            out.extend("\n" for _ in range(block.count("\n")))
            i = end + 2
            continue
        out.append(ch)
        i += 1

    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before } or ] outside JSON strings."""
    out: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    escaped = False
    while i < length:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
        elif ch == ",":
            j = i + 1
            while j < length and text[j].isspace():
                j += 1
            if j < length and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1

    return "".join(out)


def load_config(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None

    # The example config uses JSONC so tile examples can be commented out.
    try:
        text = _strip_trailing_commas(_strip_json_comments(text))
    except ValueError as exc:
        raise ConfigError(f"Config file has an invalid comment: {path}: {exc}") from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON (comments are allowed): {path}: {exc}") from None

    if not isinstance(raw, dict):
        raise ConfigError("The config root must be a JSON object")

    cfg: dict[str, Any] = dict(DEFAULTS)
    cfg.update(raw)

    url = str(cfg["netdata_url"]).strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("netdata_url must start with http:// or https://")
    cfg["netdata_url"] = url

    cfg["poll_seconds"] = _require_number(cfg, "poll_seconds", 2.0)
    if cfg["poll_seconds"] < 1.0:
        raise ConfigError("poll_seconds must be at least 1.0")
    cfg["netdata_timeout"] = _require_number(cfg, "netdata_timeout", 5.0)
    if cfg["netdata_timeout"] <= 0:
        raise ConfigError("netdata_timeout must be positive")

    llama_url = str(cfg["llama_url"]).strip().rstrip("/")
    if not llama_url.startswith(("http://", "https://")):
        raise ConfigError("llama_url must start with http:// or https://")
    cfg["llama_url"] = llama_url
    cfg["llama_api_key"] = str(cfg.get("llama_api_key") or "")
    cfg["llama_timeout"] = _require_number(cfg, "llama_timeout", 5.0)
    if cfg["llama_timeout"] <= 0:
        raise ConfigError("llama_timeout must be positive")
    cfg["llama_model_ttl"] = _require_number(cfg, "llama_model_ttl", 10.0)
    if cfg["llama_model_ttl"] < 0:
        raise ConfigError("llama_model_ttl must be zero or positive")

    ha_url = str(cfg["ha_url"]).strip().rstrip("/")
    if not ha_url.startswith(("http://", "https://")):
        raise ConfigError("ha_url must start with http:// or https://")
    cfg["ha_url"] = ha_url
    ha_token = str(cfg.get("ha_token") or "").strip() or os.environ.get("HA_TOKEN", "").strip()
    cfg["ha_token"] = ha_token
    cfg["ha_timeout"] = _require_number(cfg, "ha_timeout", 5.0)
    if cfg["ha_timeout"] <= 0:
        raise ConfigError("ha_timeout must be positive")

    graph_length = int(_require_number(cfg, "graph_length", 120))
    if not 1 <= graph_length <= 100000:
        raise ConfigError("graph_length must be between 1 and 100000")
    cfg["graph_length"] = graph_length

    time_unit = cfg.get("time_unit")
    if time_unit is not None:
        time_unit = str(time_unit).strip()
        if any(ch in time_unit for ch in "\r\n\t"):
            raise ConfigError("time_unit must be one line")
        cfg["time_unit"] = time_unit or None

    tiles_raw = cfg.get("tiles")
    if not isinstance(tiles_raw, list):
        raise ConfigError("tiles must be an array")
    if len(tiles_raw) > 4:
        raise ConfigError("Polymath supports at most four top-bar tiles (indexes 0-3)")
    if not tiles_raw:
        LOG.warning(
            "No tiles configured in %s; uncomment at least one tile in the config",
            path,
        )

    tiles: list[TileConfig] = []
    seen_indexes: set[int] = set()
    seen_titles: set[str] = set()
    for position, item in enumerate(tiles_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"tiles[{position}] must be an object")
        try:
            index = int(item["index"])
            title = str(item["title"]).strip()
            metric = str(item["metric"]).strip().lower()
        except KeyError as exc:
            raise ConfigError(f"tiles[{position}] is missing {exc.args[0]!r}") from None
        except (TypeError, ValueError):
            raise ConfigError(f"tiles[{position}].index must be an integer") from None

        if not 0 <= index <= 3:
            raise ConfigError(f"tiles[{position}].index must be between 0 and 3")
        if index in seen_indexes:
            raise ConfigError(f"Duplicate tile index {index}")
        if not title:
            raise ConfigError(f"tiles[{position}].title must not be empty")
        if "\n" in title or "\t" in title:
            raise ConfigError(f"tiles[{position}].title must be one line")
        if title in seen_titles:
            raise ConfigError(f"Duplicate tile title {title!r}; the title is the tile ID")
        if metric not in KNOWN_METRICS:
            raise ConfigError(
                f"tiles[{position}].metric must be one of {', '.join(sorted(KNOWN_METRICS))}"
            )

        # Per-tile timing.  Omitted (or null) fields inherit the global value.
        if item.get("poll_seconds") is not None:
            tile_poll = _tile_number(item, "poll_seconds", position, cfg["poll_seconds"])
            if tile_poll < 1.0:
                raise ConfigError(f"tiles[{position}].poll_seconds must be at least 1.0")
        else:
            tile_poll = float(cfg["poll_seconds"])

        graph_length_explicit = item.get("graph_length") is not None
        if graph_length_explicit:
            raw_length = _tile_number(
                item, "graph_length", position, cfg["graph_length"]
            )
            tile_graph_length = int(raw_length)
            if not 1 <= tile_graph_length <= 100000:
                raise ConfigError(
                    f"tiles[{position}].graph_length must be between 1 and 100000"
                )
        else:
            tile_graph_length = int(cfg["graph_length"])

        if "time_unit" in item:
            raw_unit = item.get("time_unit")
            tile_time_unit = str(raw_unit).strip() if raw_unit is not None else ""
            if any(ch in tile_time_unit for ch in "\r\n\t"):
                raise ConfigError(f"tiles[{position}].time_unit must be one line")
            tile_time_unit = tile_time_unit or derive_time_unit(tile_poll)
        elif cfg.get("time_unit") and tile_poll == float(cfg["poll_seconds"]):
            # An explicit global label still applies to tiles that keep the
            # global interval; tiles polled at a different rate get a label
            # that matches their own x-axis.
            tile_time_unit = str(cfg["time_unit"])
        else:
            tile_time_unit = derive_time_unit(tile_poll)

        chart = item.get("chart")
        chart = str(chart).strip() if chart is not None else None
        if metric in {"disk", "temp", "custom"} and not chart:
            raise ConfigError(f"tiles[{position}] metric={metric!r} needs a 'chart'")
        if chart and any(ch in chart for ch in "\r\n\t"):
            raise ConfigError(f"tiles[{position}].chart must be one line")

        dimension = item.get("dimension")
        dimension = str(dimension).strip() if dimension is not None else None

        entity = item.get("entity")
        entity = str(entity).strip() if entity is not None else None
        attribute = item.get("attribute")
        attribute = str(attribute).strip() if attribute is not None else None
        if metric == "hass":
            if not entity:
                raise ConfigError(f"tiles[{position}] metric='hass' needs an 'entity'")
            if any(ch in entity for ch in "\r\n\t"):
                raise ConfigError(f"tiles[{position}].entity must be one line")
            if "." not in entity:
                raise ConfigError(
                    f"tiles[{position}].entity must be a full entity id, e.g. sensor.example"
                )
        elif entity is not None or attribute is not None:
            raise ConfigError(
                f"tiles[{position}]: 'entity'/'attribute' are only valid with metric='hass'"
            )
        if attribute is not None and any(ch in attribute for ch in "\r\n\t"):
            raise ConfigError(f"tiles[{position}].attribute must be one line")

        label = item.get("label")
        label = str(label).strip() if label is not None else None
        unit = item.get("unit")
        unit = str(unit) if unit is not None else None
        secondary = item.get("secondary")
        secondary = str(secondary) if secondary is not None else None

        try:
            scale = float(item.get("scale", 1.0))
        except (TypeError, ValueError):
            raise ConfigError(f"tiles[{position}].scale must be a number") from None

        warn = item.get("warn")
        crit = item.get("crit")
        warn = float(warn) if warn is not None else None
        crit = float(crit) if crit is not None else None

        seen_indexes.add(index)
        seen_titles.add(title)
        tiles.append(
            TileConfig(
                index=index,
                title=title,
                metric=metric,
                chart=chart,
                dimension=dimension,
                entity=entity,
                attribute=attribute,
                label=label,
                unit=unit,
                scale=scale,
                secondary=secondary,
                warn=warn,
                crit=crit,
                poll_seconds=tile_poll,
                graph_length=tile_graph_length,
                time_unit=tile_time_unit,
                graph_length_explicit=graph_length_explicit,
            )
        )

    cfg["tiles"] = sorted(tiles, key=lambda t: t.index)
    return cfg


class NetdataClient:
    def __init__(self, base_url: str, timeout: float = 5.0, insecure: bool = False, ca: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.insecure = insecure
        self.ca = expand_path(ca) if ca else None
        self._ssl_context: ssl.SSLContext | None = None

    def _context(self) -> ssl.SSLContext | None:
        if not self.base_url.startswith("https://"):
            return None
        if self._ssl_context is None:
            if self.insecure:
                self._ssl_context = ssl._create_unverified_context()
            elif self.ca:
                self._ssl_context = ssl.create_default_context(cafile=str(self.ca))
            else:
                self._ssl_context = ssl.create_default_context()
        return self._ssl_context

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": f"{APP_NAME}/{VERSION}"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._context()
            ) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise NetdataError(f"Netdata HTTP {exc.code} for {url}: {compact(body, 200)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetdataError(f"Netdata request failed for {url}: {exc}") from None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise NetdataError(f"Netdata returned non-JSON for {url}: {compact(raw, 120)}") from None

    def info(self) -> dict[str, Any]:
        payload = self._get_json("/api/v1/info")
        if not isinstance(payload, dict):
            raise NetdataError("Netdata /api/v1/info returned an unexpected payload")
        return payload

    def charts(self) -> dict[str, Any]:
        payload = self._get_json("/api/v1/charts")
        if not isinstance(payload, dict) or not isinstance(payload.get("charts"), dict):
            raise NetdataError("Netdata /api/v1/charts returned an unexpected payload")
        return payload["charts"]

    def sample(self, chart: str) -> dict[str, float]:
        payload = self._get_json(
            "/api/v1/data",
            {
                "chart": chart,
                "after": -1,
                "points": 1,
                "group": "average",
                "format": "json",
            },
        )
        if not isinstance(payload, dict):
            raise NetdataError(f"Netdata data for {chart!r} is not an object")
        labels = payload.get("labels")
        rows = payload.get("data")
        if not isinstance(labels, list) or not isinstance(rows, list) or not rows:
            raise NetdataError(f"Netdata has no data for chart {chart!r}")
        row = rows[-1]
        if not isinstance(row, list) or len(row) != len(labels):
            raise NetdataError(f"Netdata returned a malformed row for chart {chart!r}")

        values: dict[str, float] = {}
        for label, raw_value in zip(labels, row):
            if label == "time":
                continue
            try:
                values[str(label)] = float(raw_value)
            except (TypeError, ValueError):
                continue
        if not values:
            raise NetdataError(f"Netdata returned no numeric dimensions for chart {chart!r}")
        return values


class LlamaClient:
    """Minimal client for llama.cpp's router/OpenAI endpoints.

    The router exposes /v1/models and needs ?model=<id> on /metrics.  Only the
    loaded model is queried; asking for an unloaded model can make the router
    autoload it (and evict the current one).

    The /metrics rate gauges only see data when a request finishes: llama.cpp
    flushes a slot's generation stats into the bucket when the slot is
    released, so during a generation both the gauges and the cumulative
    counters are frozen, and one completion spike is all you get.  To graph
    live throughput, this client samples /slots (whose per-slot
    ``next_token[0].n_decoded`` and ``n_prompt_tokens_processed`` advance in
    real time) and differentiates the counters between polls, keyed by task id
    so a new request never produces a bogus jump.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 5.0, model_ttl: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.model_ttl = model_ttl
        self._model: str | None = None
        self._model_checked = 0.0
        self._slots_supported = True
        self._slots_warned = 0.0
        self._prev_slots: dict[int, tuple[Any, float, float, float]] = {}
        self._last_slot_poll = 0.0
        self._rates_cache: dict[str, float] | None = None
        self._rates_cache_t = 0.0
        self._held: tuple[float, float, float] | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": f"{APP_NAME}/{VERSION}"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 401:
                raise LlamaError("llama.cpp rejected the API key (HTTP 401)") from None
            if exc.code == 403:
                raise LlamaError("llama.cpp denied the request (HTTP 403)") from None
            raise LlamaError(f"llama.cpp HTTP {exc.code} for {url}: {compact(body, 180)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LlamaError(f"llama.cpp request failed for {url}: {exc}") from None

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        raw = self._get_text(path, params)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise LlamaError(f"llama.cpp returned non-JSON for {path}: {compact(raw, 120)}") from None

    def loaded_model(self) -> str | None:
        now = time.monotonic()
        if self.model_ttl > 0 and self._model_checked and now - self._model_checked < self.model_ttl:
            return self._model

        payload = self._get_json("/v1/models")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise LlamaError("llama.cpp /v1/models returned an unexpected payload")

        loaded: list[str] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("id") or "").strip()
            if not model_id:
                continue
            status = entry.get("status")
            state = status.get("value") if isinstance(status, dict) else status
            if state == "loaded":
                loaded.append(model_id)

        # A plain llama-server (not the router) may omit status when it has one model.
        if not loaded and len(data) == 1 and isinstance(data[0], dict):
            only = str(data[0].get("id") or "").strip()
            if only:
                loaded.append(only)

        self._model = loaded[0] if loaded else None
        self._model_checked = now
        return self._model

    def metrics(self, model: str) -> dict[str, float]:
        raw = self._get_text("/metrics", {"model": model})
        values: dict[str, float] = {}
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[0]
            if "{" in name:
                continue
            try:
                values[name] = float(parts[-1])
            except (TypeError, ValueError):
                continue
        if not values:
            raise LlamaError("llama.cpp /metrics returned no numeric metrics")
        return values

    def slots(self, model: str) -> list[dict[str, Any]] | None:
        """Per-slot progress from GET /slots, or None when not usable right now.

        Only a server that genuinely lacks the endpoint (HTTP 404/405) turns
        /slots off for good.  Anything else - a timeout, a connection reset, a
        hiccup payload while several slots are busy - is a transient miss:
        return None for this poll and keep trying, so a single slow response
        under load can never switch the live rates off permanently and leave
        the tile at 0.0 through every later generation.
        """
        if not self._slots_supported:
            return None
        try:
            payload = self._get_json("/slots", {"model": model})
        except LlamaError as exc:
            text = str(exc)
            if "HTTP 404" in text or "HTTP 405" in text:
                self._slots_supported = False
                LOG.info("llama.cpp has no /slots endpoint; using /metrics only")
            else:
                now = time.monotonic()
                if now - self._slots_warned >= 30.0:
                    self._slots_warned = now
                    LOG.warning("llama.cpp /slots unavailable this poll (retrying): %s", compact(text, 140))
            return None
        if not isinstance(payload, list):
            now = time.monotonic()
            if now - self._slots_warned >= 30.0:
                self._slots_warned = now
                LOG.warning("llama.cpp /slots returned unexpected data (retrying)")
            return None
        return payload

    def _slot_deltas(
        self,
        slots: list[dict[str, Any]],
        previous: dict[int, tuple[Any, float, float, float]],
        dt: float,
        now: float,
    ) -> tuple[float, float, dict[int, tuple[Any, float, float, float]], bool]:
        """One differentiation pass: returns (pp, tg, current_slots, processing).

        ``n_prompt_tokens_processed`` and ``n_decoded`` advance in real time
        and restart at 0 for a new task, so a task seen for the first time
        baselines at 0 over the whole window.  Negative jumps (new task,
        reset slot) clamp to 0 and never leak across requests.
        """
        current: dict[int, tuple[Any, float, float, float]] = {}
        pp = 0.0
        tg = 0.0
        processing = False

        for slot in slots:
            if not isinstance(slot, dict):
                continue
            try:
                slot_id = int(slot["id"])
            except (KeyError, TypeError, ValueError):
                continue

            decoded = 0.0
            next_token = slot.get("next_token")
            if isinstance(next_token, list):
                for item in next_token:
                    if isinstance(item, dict):
                        try:
                            decoded += float(item.get("n_decoded") or 0.0)
                        except (TypeError, ValueError):
                            continue

            try:
                prompt_done = float(slot.get("n_prompt_tokens_processed") or 0.0)
            except (TypeError, ValueError):
                prompt_done = 0.0

            task_id = slot.get("id_task")
            current[slot_id] = (task_id, decoded, prompt_done, now)

            if not slot.get("is_processing"):
                continue
            processing = True
            if dt <= 0:
                continue
            before = previous.get(slot_id)
            if before is None or before[0] != task_id:
                base_dec, base_prompt = 0.0, 0.0  # new task: counters start at 0
            else:
                base_dec, base_prompt = before[1], before[2]
            tg += max(0.0, decoded - base_dec) / dt
            pp += max(0.0, prompt_done - base_prompt) / dt

        return pp, tg, current, processing

    def live_rates(self, model: str) -> dict[str, float] | None:
        """Rate per second derived from the live slot counters between polls.

        Returns keys ``pp`` and ``tg`` in tokens/s, or None when no request is
        in flight.  When every active slot looks frozen (typically the first
        poll window of a fresh request, before its first decode batch has
        landed), one short immediate re-sample is taken so a generating tile
        never paints 0.0 while tokens are actually moving.
        """
        now = time.monotonic()
        if self._rates_cache is not None and now - self._rates_cache_t < 0.5:
            return self._rates_cache

        slots = self.slots(model)
        if slots is None:
            return None

        dt = now - self._last_slot_poll if self._last_slot_poll else 0.0
        pp, tg, current, processing = self._slot_deltas(slots, self._prev_slots, dt, now)

        if processing and pp <= 0.0 and tg <= 0.0:
            time.sleep(0.4)
            again = self.slots(model)
            if again is not None:
                now2 = time.monotonic()
                pp, tg, current, _ = self._slot_deltas(again, current, now2 - now, now2)
                now = now2

        self._prev_slots = current
        self._last_slot_poll = now
        result = {"pp": pp, "tg": tg} if (pp > 0 or tg > 0) else None
        self._rates_cache = result
        self._rates_cache_t = now
        return result


class HassClient:
    """Minimal read-only client for the Home Assistant REST API.

    Uses a long-lived access token and only reads entity states
    (GET /api/states/...); it never calls services.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout

    def _get_json(self, path: str) -> Any:
        if not self.token:
            raise HassError(
                "No Home Assistant token: set ha_token in the config or the HA_TOKEN env var"
            )
        url = self.base_url + path
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "User-Agent": f"{APP_NAME}/{VERSION}",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise HassError(f"Home Assistant rejected the token (HTTP {exc.code})") from None
            raise HassError(f"Home Assistant HTTP {exc.code} for {url}: {compact(body, 180)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HassError(f"Home Assistant request failed for {url}: {exc}") from None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise HassError(f"Home Assistant returned non-JSON for {path}: {compact(raw, 120)}") from None

    def state(self, entity_id: str) -> dict[str, Any]:
        path = "/api/states/" + urllib.parse.quote(entity_id, safe=".")
        try:
            payload = self._get_json(path)
        except HassError as exc:
            if "HTTP 404" in str(exc):
                raise HassError(f"Home Assistant has no entity {entity_id!r}") from None
            raise
        if not isinstance(payload, dict) or "state" not in payload:
            raise HassError(f"Home Assistant returned an unexpected payload for {entity_id!r}")
        return payload

    def states(self) -> list[dict[str, Any]]:
        payload = self._get_json("/api/states")
        if not isinstance(payload, list):
            raise HassError("Home Assistant /api/states returned an unexpected payload")
        return payload


def _hass_to_number(raw: Any, what: str) -> float:
    """Coerce an HA state or attribute to a float the graph can plot."""
    if raw is None:
        raise HassError(f"{what} has no value")
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().lower()
    if text in ("", "unknown", "unavailable"):
        raise HassError(f"{what} is {text or 'empty'}")
    if text == "on":
        return 1.0
    if text == "off":
        return 0.0
    try:
        return float(text)
    except ValueError:
        raise HassError(f"{what} is not numeric: {compact(str(raw), 60)}") from None


def _server_info(nd: NetdataClient, fallback_name: str) -> ServerInfo:
    try:
        info = nd.info()
    except NetdataError as exc:
        LOG.warning("Could not read Netdata info: %s", exc)
        return ServerInfo(name=fallback_name, cores=None)

    name = ""
    hosts = info.get("mirrored_hosts")
    if isinstance(hosts, list) and hosts:
        name = str(hosts[0])
    if not name:
        name = str(info.get("hostname") or "")
    cores: int | None = None
    try:
        raw_cores = info.get("cores_total")
        if raw_cores is not None:
            cores = int(raw_cores)
    except (TypeError, ValueError):
        cores = None
    return ServerInfo(name=name or fallback_name, cores=cores)


def discover_gpu_chart(nd: NetdataClient) -> str | None:
    try:
        charts = nd.charts()
    except NetdataError:
        return None
    candidates = sorted(
        chart_id
        for chart_id in charts
        if chart_id.startswith("nvidia_smi.") and chart_id.endswith("_gpu_utilization")
    )
    return candidates[0] if candidates else None


def _mount_from_chart(chart: str) -> str:
    if chart.startswith("disk_space."):
        mount = chart[len("disk_space."):]
        if mount.startswith("."):
            mount = mount[1:]
        if mount.startswith("/"):
            return mount
    return chart


def _base_fields(ctx: "RenderContext", tile: TileConfig, value: float, unit: str, secondary: str) -> dict[str, Any]:
    return {
        "value": float(value),
        "unit": unit,
        "subtitle": ctx.server_name,
        "secondary": secondary,
        "color": "Legend",
    }


@dataclass
class RenderContext:
    nd: NetdataClient
    llama: LlamaClient
    hass: HassClient
    server_name: str
    cores: int | None


def _render_cpu(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    values = ctx.nd.sample("system.cpu")
    if "idle" in values:
        value = 100.0 - values["idle"]
    else:
        # Netdata's system.cpu chart is the total utilization chart and omits idle.
        value = sum(values.values())
    value = clamp(value)

    extras: list[str] = []
    try:
        load = ctx.nd.sample("system.load")
        load1 = load.get("load1")
        if load1 is not None:
            extras.append(f"load {load1:.1f}")
    except NetdataError:
        pass
    if ctx.cores:
        extras.append(f"{ctx.cores} cores")
    secondary = " · ".join(extras) if extras else "CPU utilization"

    fields = _base_fields(ctx, tile, value, "%", secondary)
    fields["color"] = health_colour(value, 60.0, 85.0)
    return fields


def _render_ram(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    values = ctx.nd.sample("system.ram")
    free = max(0.0, values.get("free", 0.0))
    used = max(0.0, values.get("used", 0.0))
    cached = max(0.0, values.get("cached", 0.0))
    buffers = max(0.0, values.get("buffers", 0.0))
    total = free + used + cached + buffers
    if total <= 0:
        raise NetdataError("system.ram has no usable total")
    if used <= 0:
        used = max(0.0, total - free - cached - buffers)
    value = clamp(used / total * 100.0)
    secondary = f"{fmt_gib(used / 1024.0)} / {fmt_gib(total / 1024.0)} used"
    fields = _base_fields(ctx, tile, value, "%", secondary)
    fields["color"] = health_colour(value, 70.0, 90.0)
    return fields


def _render_gpu(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    if not tile.chart:
        raise NetdataError("No GPU utilization chart found (Netdata unreachable or no nvidia_smi collector)")
    values = ctx.nd.sample(tile.chart)
    dimension = tile.dimension or "gpu"
    raw = values.get(dimension)
    if raw is None:
        raw = next(iter(values.values()))
    value = clamp(raw * tile.scale)

    extras: list[str] = []
    base = tile.chart
    if base.endswith("_gpu_utilization"):
        base = base[: -len("_gpu_utilization")]
        for suffix, fmt in (("_temperature", "{:.0f} °C"), ("_power_draw", "{:.0f} W")):
            try:
                other = ctx.nd.sample(base + suffix)
                other_value = next(iter(other.values()))
                extras.append(fmt.format(other_value))
            except NetdataError:
                pass
    secondary = " · ".join(extras) if extras else "GPU utilization"
    fields = _base_fields(ctx, tile, value, "%", secondary)
    if tile.warn is not None and tile.crit is not None:
        fields["color"] = health_colour(value, tile.warn, tile.crit)
    else:
        fields["color"] = health_colour(value, 80.0, 95.0)
    return fields


def _render_net(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    values = ctx.nd.sample("system.net")
    down = abs(values.get("received", 0.0))
    up = abs(values.get("sent", 0.0))
    total_mbps = (down + up) / 1000.0
    secondary = f"↓ {down / 1000.0:.1f} · ↑ {up / 1000.0:.1f} Mbps"
    fields = _base_fields(ctx, tile, total_mbps, "Mbps", secondary)
    fields["color"] = "Cyan"
    return fields


def _llama_rates(values: dict[str, float]) -> tuple[float, float]:
    pp = values.get("llamacpp:prompt_tokens_seconds")
    tg = values.get("llamacpp:predicted_tokens_seconds")

    if pp is None:
        tokens = values.get("llamacpp:prompt_tokens_total")
        seconds = values.get("llamacpp:prompt_seconds_total")
        if tokens is not None and seconds and seconds > 0:
            pp = tokens / seconds
    if tg is None:
        tokens = values.get("llamacpp:tokens_predicted_total")
        seconds = values.get("llamacpp:tokens_predicted_seconds_total")
        if tokens is not None and seconds and seconds > 0:
            tg = tokens / seconds

    return max(0.0, pp or 0.0), max(0.0, tg or 0.0)


def _render_llama(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    model = ctx.llama.loaded_model()
    if not model:
        return {
            "value": 0.0,
            "unit": "tok/s",
            "subtitle": "no model loaded",
            "secondary": "pp 0.0 · tg 0.0",
            "color": "Legend",
        }

    live = ctx.llama.live_rates(model)
    values = ctx.llama.metrics(model)
    pp, tg = _llama_rates(values)

    # A completion spike from /metrics must not hide the live per-second rate,
    # and the live rate only exists while a slot reports itself processing.
    if live is not None:
        pp = live["pp"] if live["pp"] > 0 else pp
        tg = live["tg"] if live["tg"] > 0 else tg

    total = pp + tg
    fields = _base_fields(ctx, tile, total, "tok/s", f"pp {pp:.1f} · tg {tg:.1f}")
    fields["subtitle"] = compact(model, 48)
    fields["color"] = "Cyan" if total > 0 else "Legend"
    return fields


def _render_disk(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    assert tile.chart
    values = ctx.nd.sample(tile.chart)
    used = values.get("used")
    avail = values.get("avail")
    if used is None or avail is None:
        raise NetdataError(f"{tile.chart!r} has no used/avail dimensions")
    used = max(0.0, used)
    avail = max(0.0, avail)
    total = used + avail
    if total <= 0:
        raise NetdataError(f"{tile.chart!r} has no usable total")
    value = clamp(used / total * 100.0)
    mount = tile.label or _mount_from_chart(tile.chart)
    secondary = f"{fmt_gib(used)} / {fmt_gib(total)} used"
    fields = _base_fields(ctx, tile, value, "%", secondary)
    fields["subtitle"] = f"{ctx.server_name} · {mount}"
    fields["color"] = health_colour(value, 80.0, 90.0)
    return fields


def _render_temp(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    assert tile.chart
    values = ctx.nd.sample(tile.chart)
    if tile.dimension:
        raw = values.get(tile.dimension)
        if raw is None:
            raise NetdataError(f"{tile.chart!r} has no dimension {tile.dimension!r}")
    else:
        raw = next(iter(values.values()))
    value = raw * tile.scale
    unit = tile.unit or "°C"
    secondary = tile.secondary or "temperature"
    fields = _base_fields(ctx, tile, value, unit, secondary)
    if tile.warn is not None and tile.crit is not None:
        fields["color"] = health_colour(value, tile.warn, tile.crit)
    else:
        fields["color"] = health_colour(value, 70.0, 85.0)
    return fields


def _render_custom(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    assert tile.chart
    values = ctx.nd.sample(tile.chart)
    if tile.dimension:
        raw = values.get(tile.dimension)
        if raw is None:
            raise NetdataError(f"{tile.chart!r} has no dimension {tile.dimension!r}")
    else:
        raw = next(iter(values.values()))
    value = raw * tile.scale
    unit = tile.unit or ""
    secondary = tile.secondary or tile.chart
    fields = _base_fields(ctx, tile, value, unit, secondary)
    if tile.warn is not None and tile.crit is not None:
        fields["color"] = health_colour(value, tile.warn, tile.crit)
    return fields


def _render_hass(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    assert tile.entity
    payload = ctx.hass.state(tile.entity)
    attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}

    if tile.attribute:
        if tile.attribute not in attrs:
            raise HassError(f"{tile.entity!r} has no attribute {tile.attribute!r}")
        raw = attrs[tile.attribute]
        what = f"{tile.entity}.{tile.attribute}"
    else:
        raw = payload.get("state")
        what = tile.entity

    value = _hass_to_number(raw, what) * tile.scale
    unit = tile.unit or str(attrs.get("unit_of_measurement") or "")
    if tile.secondary:
        secondary = tile.secondary
    elif tile.attribute:
        secondary = f"{tile.entity} · {tile.attribute}"
    else:
        secondary = str(attrs.get("friendly_name") or tile.entity)

    fields = _base_fields(ctx, tile, value, unit, secondary)
    if tile.warn is not None and tile.crit is not None:
        fields["color"] = health_colour(value, tile.warn, tile.crit)
    return fields


RENDERERS = {
    "cpu": _render_cpu,
    "ram": _render_ram,
    "gpu": _render_gpu,
    "llama": _render_llama,
    "net": _render_net,
    "disk": _render_disk,
    "temp": _render_temp,
    "custom": _render_custom,
    "hass": _render_hass,
}


def render_tile(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    renderer = RENDERERS[tile.metric]
    return renderer(tile, ctx)


def offline_fields(tile: TileConfig, error: str) -> dict[str, Any]:
    if tile.last_fields:
        fields = dict(tile.last_fields)
        fields["subtitle"] = "OFFLINE"
        last = datetime.fromtimestamp(tile.last_ok).strftime("%H:%M:%S") if tile.last_ok else "unknown"
        fields["secondary"] = f"last {last} · {compact(error, 40)}"
        fields["color"] = "Red"
        return fields
    return {
        "value": 0.0,
        "unit": tile.unit or "",
        "subtitle": "OFFLINE",
        "secondary": compact(error, 60),
        "color": "Red",
    }


class PolymathClient:
    def __init__(self, cfg: dict[str, Any]):
        self.keys_path = expand_path(cfg["keys_json"])
        self.api_key_path = expand_path(cfg["api_key_file"])
        self.user_agent = str(cfg["user_agent"])
        self.port: int | None = None
        self.ssl_context: ssl.SSLContext | None = None
        self.api_key: str | None = None
        self.needs_save = False

    def _read_keys(self) -> dict[str, Any]:
        try:
            keys = json.loads(self.keys_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PolymathNotReady(
                f"{self.keys_path} not found. Start Polymath once and enable "
                "Settings > API, then try again."
            ) from None
        except (OSError, json.JSONDecodeError) as exc:
            raise PolymathNotReady(f"Cannot read {self.keys_path}: {exc}") from None

        if not keys.get("apiActive"):
            raise PolymathNotReady("The Polymath API is disabled (enable it in Settings > API)")
        port = keys.get("openPort")
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise PolymathNotReady(f"Invalid openPort in {self.keys_path}: {port!r}") from None

        cert_location = keys.get("publicKeyLocation")
        candidates: list[Path] = []
        if cert_location:
            candidates.append(expand_path(cert_location))
        candidates.append(expand_path("~/.local/share/com.fluxkeyboard.polymath/tls/cert.pem"))
        cert: Path | None = None
        for candidate in candidates:
            if candidate.is_dir():
                candidate = candidate / "cert.pem"
            if candidate.is_file():
                cert = candidate
                break
        if cert is None:
            raise PolymathNotReady(
                "Could not find Polymath's TLS certificate. Check publicKeyLocation in "
                f"{self.keys_path}."
            )

        self.port = port
        try:
            self.ssl_context = ssl.create_default_context(cafile=str(cert))
        except (OSError, ssl.SSLError) as exc:
            raise PolymathNotReady(f"Cannot load Polymath TLS certificate {cert}: {exc}") from None
        return keys

    def _load_key(self) -> str | None:
        env_key = os.environ.get("POLYMATH_API_KEY")
        if env_key:
            return env_key.strip()
        try:
            key = self.api_key_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            LOG.warning("Could not read %s: %s", self.api_key_path, exc)
            return None
        return key or None

    def _save_key(self, key: str) -> None:
        self.api_key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.api_key_path.parent, 0o700)
        except OSError:
            pass
        self.api_key_path.write_text(key + "\n", encoding="utf-8")
        try:
            os.chmod(self.api_key_path, 0o600)
        except OSError:
            pass

    def _delete_key(self) -> None:
        try:
            self.api_key_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOG.warning("Could not remove %s: %s", self.api_key_path, exc)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
        auth: bool = True,
    ) -> Any:
        if self.port is None:
            raise PolymathNotReady("Polymath API port is not known yet")
        url = f"https://localhost:{self.port}/v1{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("User-Agent", self.user_agent)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if auth and self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=self.ssl_context
            ) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise ApiAuthError(exc.code, body) from None
            raise ApiError(exc.code, body) from None
        except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            raise PolymathError(f"Polymath request failed for {url}: {exc}") from None

        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def connect(self) -> None:
        self._read_keys()
        # /docs is unauthenticated and is the cheapest "is it up?" check.
        self._request("GET", "/docs", auth=False, timeout=10.0)

        self.api_key = self._load_key()
        if self.api_key:
            try:
                self._request("GET", "/authentication/check")
                return
            except ApiAuthError as exc:
                if exc.status != 401:
                    raise
                LOG.info("The stored Polymath API key is no longer valid; requesting a new one")
                self.api_key = None
                self._delete_key()

        LOG.info("Requesting a Polymath API key; approve the prompt in Polymath")
        result = self._request("POST", "/authentication/register", {}, timeout=180.0, auth=False)
        if not isinstance(result, dict):
            raise PolymathError(f"Unexpected registration response: {result!r}")
        key = str(result.get("message") or "").strip()
        if not key:
            raise PolymathError("Polymath did not return an API key")
        self.api_key = key
        self._save_key(key)
        LOG.info("Polymath API key stored in %s", self.api_key_path)

    def _check_message(self, result: Any) -> str:
        if isinstance(result, dict):
            message = str(result.get("message") or "")
        else:
            message = str(result or "")
        lowered = message.lower()
        if "does not exist" in lowered:
            raise TileMissing(message)
        if "error" in lowered or "failed" in lowered or "invalid" in lowered:
            raise ApiError(200, message)
        return message

    def update_tile(self, tile: TileConfig, fields: dict[str, Any]) -> None:
        body: dict[str, Any] = {
            "id": tile.id or tile.title,
            "currentValue": float(round(float(fields["value"]), 2)),
        }
        if fields.get("unit"):
            body["unit"] = fields["unit"]
        if fields.get("subtitle"):
            body["subTitle"] = fields["subtitle"]
        if fields.get("secondary"):
            body["secondarySubtitle"] = fields["secondary"]
        if fields.get("color"):
            body["color"] = fields["color"]
        if tile.time_unit:
            body["timeUnit"] = tile.time_unit
        result = self._request("PUT", "/config/updateGenericTile", body)
        self._check_message(result)

    def create_tile(self, tile: TileConfig) -> None:
        body = {
            "title": tile.title,
            "type": TILE_TYPE_GRAPH,
            "index": tile.index,
            "length": tile.graph_length,
        }
        result = self._request("POST", "/config/addGenericTile", body, timeout=60.0)
        message = self._check_message(result)
        tile.id = message.strip() or tile.title
        tile.applied_graph_length = tile.graph_length
        LOG.info(
            "Created tile %r at index %d with %d points (id=%r)",
            tile.title,
            tile.index,
            tile.graph_length,
            tile.id,
        )

    def _graph_length_matches(self, tile: TileConfig) -> bool:
        """Whether the tile already has the configured graph length.

        Polymath's update endpoint cannot change ``length``; only
        addGenericTile accepts it.  Track what was last applied in the state
        file so a length change replaces the tile once, rather than on every
        start.
        """
        if tile.applied_graph_length is not None:
            return tile.applied_graph_length == tile.graph_length
        if tile.graph_length_explicit:
            # State files from older versions have no recorded length.  Honour an
            # explicit per-tile value once, even though the recreate clears
            # that tile's graph history; the state file prevents repeats.
            return False
        # No record and no explicit length: assume the existing tile was
        # created with the configured length (the old global-only behaviour).
        tile.applied_graph_length = tile.graph_length
        return True

    def ensure_tile(self, tile: TileConfig) -> bool:
        tile.id = tile.id or tile.title
        try:
            # A successful update proves the tile still exists.  Value 0 only
            # seeds the graph; the real value is sent immediately afterwards.
            self.update_tile(
                tile,
                {"value": 0.0, "unit": tile.unit or "", "subtitle": "starting", "color": "Legend"},
            )
            if self._graph_length_matches(tile):
                return False
            LOG.info(
                "Tile %r graph length is %s; recreating it with %d points",
                tile.title,
                tile.applied_graph_length if tile.applied_graph_length is not None else "unknown",
                tile.graph_length,
            )
        except TileMissing:
            pass
        except ApiError as exc:
            if exc.status != 500:
                raise
            LOG.info("Tile %r exists but is not usable (%s); recreating it", tile.title, exc)
        self.create_tile(tile)
        return True

    def setup_tiles(self, tiles: list[TileConfig]) -> None:
        created = False
        for tile in tiles:
            try:
                if self.ensure_tile(tile):
                    created = True
            except ApiError as exc:
                raise PolymathError(f"Could not prepare tile {tile.title!r}: {exc}") from None
        if created:
            try:
                self.save()
            except PolymathError as exc:
                LOG.warning("Tiles were created but could not be saved to the keyboard: %s", exc)
                self.needs_save = True

    def save(self) -> None:
        result = self._request("POST", "/config/save", {}, timeout=60.0)
        self._check_message(result)
        self.needs_save = False
        LOG.info("Saved the keyboard configuration")

    def retry_save(self) -> None:
        if not self.needs_save:
            return
        try:
            self.save()
        except PolymathError as exc:
            LOG.warning("Still could not save the keyboard configuration: %s", exc)


def load_state(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def tile_state_key(tile: TileConfig) -> str:
    return f"{tile.index}:{tile.title}"


def tile_length_state_key(tile: TileConfig) -> str:
    return f"length:{tile.index}:{tile.title}"


def save_state(path: Path, tiles: list[TileConfig]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {tile_state_key(tile): tile.id for tile in tiles if tile.id}
        for tile in tiles:
            if tile.applied_graph_length is not None:
                state[tile_length_state_key(tile)] = str(tile.applied_graph_length)
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        LOG.warning("Could not write state file %s: %s", path, exc)


class Bridge:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.nd = NetdataClient(
            str(cfg["netdata_url"]),
            timeout=float(cfg["netdata_timeout"]),
            insecure=bool(cfg["netdata_insecure"]),
            ca=cfg.get("netdata_ca"),
        )
        self.llama = LlamaClient(
            str(cfg["llama_url"]),
            str(cfg.get("llama_api_key") or ""),
            timeout=float(cfg["llama_timeout"]),
            model_ttl=float(cfg["llama_model_ttl"]),
        )
        self.poly = PolymathClient(cfg)
        self.hass = HassClient(
            str(cfg["ha_url"]),
            str(cfg.get("ha_token") or ""),
            timeout=float(cfg["ha_timeout"]),
        )
        self.tiles: list[TileConfig] = list(cfg["tiles"])
        self.state_path = expand_path(cfg["state_file"])
        self.state = load_state(self.state_path)
        for tile in self.tiles:
            tile.id = self.state.get(tile_state_key(tile), "")
            raw_length = self.state.get(tile_length_state_key(tile), "")
            if raw_length.isdigit():
                tile.applied_graph_length = int(raw_length)
        self.server = ServerInfo()
        self.ctx: RenderContext | None = None
        self.connected = False
        self.next_save_retry = 0.0
        self.next_gpu_discovery = 0.0

    def prepare(self) -> None:
        parsed = urllib.parse.urlparse(str(self.cfg["netdata_url"]))
        fallback = parsed.hostname or "server"
        info = _server_info(self.nd, fallback)
        name = str(self.cfg.get("server_name") or "").strip() or info.name or fallback
        self.server = ServerInfo(name=name, cores=info.cores)
        self.ctx = RenderContext(
            nd=self.nd,
            llama=self.llama,
            hass=self.hass,
            server_name=self.server.name,
            cores=self.server.cores,
        )
        for tile in self.tiles:
            if tile.metric == "gpu" and not tile.chart:
                tile.chart = discover_gpu_chart(self.nd)
                if tile.chart:
                    LOG.info("Using GPU chart %s for tile %r", tile.chart, tile.title)
                else:
                    LOG.warning("No nvidia_smi *_gpu_utilization chart found for tile %r", tile.title)
            LOG.info(
                "Tile %r: every %gs, %d points (%.1f min window, label %r)",
                tile.title,
                tile.poll_seconds,
                tile.graph_length,
                tile.graph_length * tile.poll_seconds / 60.0,
                tile.time_unit,
            )
        LOG.info(
            "Netdata %s (%s%s)",
            self.cfg["netdata_url"],
            self.server.name,
            f", {self.server.cores} cores" if self.server.cores else "",
        )
        try:
            model = self.llama.loaded_model()
            if model:
                LOG.info("llama.cpp model: %s", model)
            else:
                LOG.warning("llama.cpp has no loaded model at %s", self.cfg["llama_url"])
        except LlamaError as exc:
            LOG.warning("Could not read llama.cpp model: %s", exc)

    def _schedule_next(self, tile: TileConfig, due: float) -> None:
        """Set the tile's next deadline from the slot it was due at."""
        now = time.monotonic()
        next_due = due + tile.poll_seconds
        if next_due <= now:
            # The poll took longer than the interval; skip the missed slots
            # instead of firing a burst to catch up.
            next_due = now + tile.poll_seconds
        tile.next_poll = next_due

    def _next_wake(self) -> float:
        now = time.monotonic()
        deadlines = [tile.next_poll for tile in self.tiles]
        if self.poly.needs_save:
            # Do not let a long tile interval postpone the save retry.
            deadlines.append(self.next_save_retry)
        upcoming = [deadline for deadline in deadlines if deadline > now]
        if upcoming:
            return min(upcoming)
        # With no tiles configured, keep the loop idle instead of spinning.
        return now + max(1.0, float(self.cfg["poll_seconds"]))

    def dry_run(self, once: bool = False) -> None:
        assert self.ctx is not None
        while True:
            now = time.monotonic()
            parts: list[str] = []
            for tile in self.tiles:
                if not once and tile.next_poll and tile.next_poll > now:
                    continue
                due = tile.next_poll or now
                try:
                    fields = render_tile(tile, self.ctx)
                    value = fields["value"]
                    unit = fields["unit"]
                    subtitle = fields.get("subtitle", "")
                    parts.append(f"{tile.title}[{subtitle}]={value:.2f}{unit} ({fields['secondary']})")
                except NetdataError as exc:
                    parts.append(f"{tile.title}=OFFLINE ({compact(exc, 60)})")
                self._schedule_next(tile, due)
            if parts:
                print(" | ".join(parts), flush=True)
            if once:
                return
            time.sleep(max(0.0, self._next_wake() - time.monotonic()))

    def _setup(self) -> None:
        self.poly.connect()
        self.poly.setup_tiles(self.tiles)
        save_state(self.state_path, self.tiles)
        self.connected = True
        # Poll every tile immediately once the connection (re)opens.
        for tile in self.tiles:
            tile.next_poll = 0.0

    def _poll_tile(self, tile: TileConfig) -> None:
        assert self.ctx is not None
        if (
            tile.metric == "gpu"
            and not tile.chart
            and time.monotonic() >= self.next_gpu_discovery
        ):
            tile.chart = discover_gpu_chart(self.nd)
            self.next_gpu_discovery = time.monotonic() + 60.0
            if tile.chart:
                LOG.info("Using GPU chart %s for tile %r", tile.chart, tile.title)

        try:
            fields = render_tile(tile, self.ctx)
            tile.last_fields = fields
            tile.last_ok = time.time()
            tile.last_error = None
        except NetdataError as exc:
            error = str(exc)
            if tile.last_error != error:
                LOG.warning("Netdata metric for %r failed: %s", tile.title, error)
            tile.last_error = error
            fields = offline_fields(tile, error)

        try:
            self.poly.update_tile(tile, fields)
        except TileMissing:
            LOG.info("Tile %r is missing; recreating it", tile.title)
            try:
                self.poly.create_tile(tile)
                self.poly.needs_save = True
                self.poly.retry_save()
                self.poly.update_tile(tile, fields)
                save_state(self.state_path, self.tiles)
            except ApiAuthError as exc:
                LOG.warning("Polymath rejected the API key (HTTP %s)", exc.status)
                if exc.status == 401:
                    self.poly.api_key = None
                    self.poly._delete_key()
                self.connected = False
            except (ApiError, PolymathError) as exc:
                LOG.warning("Could not recreate tile %r: %s", tile.title, exc)
                self.connected = False
        except ApiAuthError as exc:
            LOG.warning("Polymath rejected the API key (HTTP %s)", exc.status)
            if exc.status == 401:
                self.poly.api_key = None
                self.poly._delete_key()
            self.connected = False
        except (ApiError, PolymathError) as exc:
            LOG.warning("Lost connection to Polymath: %s", exc)
            self.connected = False

    def _poll(self) -> None:
        assert self.ctx is not None
        now = time.monotonic()
        for tile in self.tiles:
            if tile.next_poll and tile.next_poll > now:
                continue
            due = tile.next_poll or now
            self._poll_tile(tile)
            self._schedule_next(tile, due)
            if not self.connected:
                return

        if self.poly.needs_save and time.monotonic() >= self.next_save_retry:
            self.poly.retry_save()
            if self.poly.needs_save:
                self.next_save_retry = time.monotonic() + 30.0

    def run(self, once: bool = False) -> None:
        self.prepare()
        while True:
            if not self.connected:
                try:
                    self._setup()
                except PolymathNotReady as exc:
                    LOG.warning("%s; retrying in 5 s", exc)
                    if once:
                        raise
                    time.sleep(5.0)
                    continue
                except ApiAuthError as exc:
                    LOG.warning("Polymath access was denied (HTTP %s); retrying in 30 s", exc.status)
                    if once:
                        raise
                    time.sleep(30.0)
                    continue
                except PolymathError as exc:
                    LOG.warning("Could not connect to Polymath: %s; retrying in 5 s", exc)
                    if once:
                        raise
                    time.sleep(5.0)
                    continue

            self._poll()
            if once:
                return
            if not self.connected:
                continue
            time.sleep(max(0.0, self._next_wake() - time.monotonic()))


def list_charts(nd: NetdataClient, needle: str) -> None:
    charts = nd.charts()
    needle = needle.lower()
    rows = []
    for chart_id, meta in charts.items():
        if not isinstance(meta, dict):
            continue
        title = str(meta.get("title") or "")
        units = str(meta.get("units") or "")
        if needle and needle not in chart_id.lower() and needle not in title.lower():
            continue
        rows.append((str(chart_id), units, title))
    for chart_id, units, title in sorted(rows):
        print(f"{chart_id}\t{units}\t{title}")
    print(f"\n{len(rows)} chart(s)", file=sys.stderr)


def list_entities(hass: HassClient, needle: str) -> None:
    states = hass.states()
    needle = needle.lower()
    rows = []
    for item in states:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "")
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        friendly = str(attrs.get("friendly_name") or "")
        if needle and needle not in entity_id.lower() and needle not in friendly.lower():
            continue
        state = str(item.get("state") if item.get("state") is not None else "")
        unit = str(attrs.get("unit_of_measurement") or "")
        rows.append((entity_id, state, unit, friendly))
    for entity_id, state, unit, friendly in sorted(rows):
        print(f"{entity_id}\t{state}\t{unit}\t{friendly}")
    print(f"\n{len(rows)} entity/entities", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="flux-netdata-bridge",
        description="Mirror Netdata metrics onto Flux Polymath graph tiles.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config JSON (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print metrics; do not talk to Polymath")
    parser.add_argument("--once", action="store_true", help="Poll once, then exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--list-charts",
        nargs="?",
        const="",
        metavar="FILTER",
        help="List Netdata chart IDs matching FILTER, then exit",
    )
    parser.add_argument(
        "--list-entities",
        nargs="?",
        const="",
        metavar="FILTER",
        help="List Home Assistant entity IDs matching FILTER, then exit",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    config_path = expand_path(args.config)
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2

    nd = NetdataClient(
        str(cfg["netdata_url"]),
        timeout=float(cfg["netdata_timeout"]),
        insecure=bool(cfg["netdata_insecure"]),
        ca=cfg.get("netdata_ca"),
    )

    if args.list_charts is not None:
        try:
            list_charts(nd, args.list_charts)
        except NetdataError as exc:
            LOG.error("%s", exc)
            return 1
        return 0

    if args.list_entities is not None:
        hass = HassClient(
            str(cfg["ha_url"]),
            str(cfg.get("ha_token") or ""),
            timeout=float(cfg["ha_timeout"]),
        )
        try:
            list_entities(hass, args.list_entities)
        except HassError as exc:
            LOG.error("%s", exc)
            return 1
        return 0

    try:
        bridge = Bridge(cfg)
        if args.dry_run:
            bridge.prepare()
            bridge.dry_run(once=args.once)
        else:
            bridge.run(once=args.once)
    except KeyboardInterrupt:
        LOG.info("Stopping")
        return 0
    except (BridgeError, ConfigError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
