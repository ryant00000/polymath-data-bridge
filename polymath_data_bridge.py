#!/usr/bin/env python3
"""Polymath Data Bridge.

Polls a Netdata agent, a llama.cpp server, a Home Assistant instance, a Plex
Media Server, and Steam's public player-count API and mirrors the metrics onto
Flux Polymath generic graph and grid tiles through Polymath's local HTTPS API.

The bridge runs on the machine that has the Flux keyboard attached.  The data
sources can live on other machines; only outbound HTTP(S) to them and localhost
HTTPS to Polymath are needed.

Standard library only.  Tested against Netdata v2.12.x, Plex Media Server
1.43.x, and the Polymath 1.5.0.5 API.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

APP_NAME = "Polymath Data Bridge"
VERSION = "1.5"
LOG = logging.getLogger("polymath-data-bridge")

DEFAULT_CONFIG_PATH = "~/.config/polymath-data-bridge/config.json"
DEFAULT_KEYS_JSON = "~/flux/config/keys.json"
DEFAULT_API_KEY_FILE = "~/.config/polymath-data-bridge/api_key"
DEFAULT_STATE_FILE = "~/.local/state/polymath-data-bridge/state.json"

# Generic tile types in Polymath 1.5.0.x.  A graph tile is one chart; a grid
# tile is one top-bar slot containing up to six compact readouts.
TILE_TYPE_GRAPH = 1
TILE_TYPE_GRID = 2
GRID_MAX_CELLS = 6

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
    "llama_api_key": "",
    "llama_timeout": 5.0,
    "llama_model_ttl": 0.0,
    "llama_insecure": False,
    "llama_ca": None,
    "hass_url": "http://127.0.0.1:8123",
    "hass_token": "",
    "hass_timeout": 5.0,
    "hass_insecure": False,
    "hass_ca": None,
    "steam_url": "https://api.steampowered.com",
    "steam_timeout": 5.0,
    "steam_insecure": False,
    "steam_ca": None,
    "plex_url": "http://127.0.0.1:32400",
    "plex_token": "",
    "plex_timeout": 5.0,
    "plex_insecure": False,
    "plex_ca": None,
    "polymath_host": "localhost",
    "tiles": [],
}

# Every tile and subtext names the service it reads from with ``source``.
# Netdata exposes several preset metrics, so its tiles also carry ``metric``.
# ``grid`` is a presentation type rather than a service; each of its cells
# names one of DATA_SOURCES.
DATA_SOURCES = {"netdata", "llama", "hass", "steam", "plex"}
TILE_SOURCES = DATA_SOURCES | {"grid"}
NETDATA_METRICS = {"cpu", "ram", "gpu", "disk", "net", "temp", "custom"}

TILE_KEYS = {
    "index",
    "title",
    "source",
    "metric",
    "chart",
    "dimension",
    "entity",
    "attribute",
    "appid",
    "label",
    "unit",
    "scale",
    "secondary",
    "warn",
    "crit",
    "eta",
    "subtexts",
    "poll_seconds",
    "graph_length",
    "time_unit",
    "start_at_zero",
    "initial_values",
    "cells",
}

# One cell of a grid tile.  A cell is a compact numeric readout, so it shares
# the tile source fields but has no graph timing, thresholds, or subtexts.
GRID_CELL_KEYS = {
    "title",
    "source",
    "metric",
    "chart",
    "dimension",
    "entity",
    "attribute",
    "appid",
    "label",
    "unit",
    "scale",
    "secondary",
    "decimals",
}

# Sources a configurable subtext line may read.  ``tile`` reuses the value
# plotted by the tile itself; the others are independent data sources.
SUBTEXT_SOURCES = {"text", "netdata", "hass", "llama", "steam", "plex", "tile"}
SUBTEXT_COMMON_KEYS = {"source", "label", "unit", "scale", "decimals", "eta"}
SUBTEXT_SOURCE_KEYS = {
    "text": {"text"},
    "netdata": {"chart", "dimension"},
    "hass": {"entity", "attribute"},
    "llama": {"field"},
    "steam": {"appid"},
    "plex": {"field"},
    "tile": set(),
}
LLAMA_FIELDS = {"total", "pp", "tg"}
PLEX_FIELDS = {"total", "direct", "transcoding", "paused"}
ETA_TARGETS = {0, 100}


class BridgeError(Exception):
    """Base class for expected bridge failures."""


class ConfigError(BridgeError):
    pass


class SourceError(BridgeError):
    """A data-source failure, with an optional HTTP status code."""

    source = "source"

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NetdataError(SourceError):
    source = "Netdata"


class LlamaError(SourceError):
    source = "llama.cpp"


class HassError(SourceError):
    source = "Home Assistant"


class SteamError(SourceError):
    source = "Steam"


class PlexError(SourceError):
    source = "Plex"


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
class SubtextSpec:
    """One of a tile's two configurable subtext lines."""

    source: str
    slot: int
    text: str | None = None
    chart: str | None = None
    dimension: str | None = None
    entity: str | None = None
    attribute: str | None = None
    appid: int | None = None
    field: str | None = None
    label: str | None = None
    unit: str | None = None
    scale: float = 1.0
    decimals: int = 2
    eta: int | None = None


@dataclass
class SeriesHistory:
    """In-memory samples used to project when a percentage hits 0 or 100."""

    max_samples: int = 50000
    samples: dict[str, list[tuple[float, float]]] = field(default_factory=dict)

    def record(self, key: str, when: float, value: float) -> None:
        series = self.samples.setdefault(key, [])
        if series and when < series[-1][0]:
            # A monotonic clock should never go backwards, but do not let a
            # synthetic/test clock mix a new run into old samples.
            series.clear()
        series.append((float(when), float(value)))
        if len(series) > self.max_samples:
            del series[: len(series) - self.max_samples]

    def eta(self, key: str, current: float, target: int, now: float) -> str:
        if target == 100 and current >= 100.0:
            return "100% reached"
        if target == 0 and current <= 0.0:
            return "0% reached"

        data = [pair for pair in (self.samples.get(key) or []) if pair[0] <= now]
        if len(data) < 5 or data[-1][0] - data[0][0] < 10.0:
            return f"{target}%: collecting"

        mean_t = sum(t for t, _ in data) / len(data)
        mean_v = sum(v for _, v in data) / len(data)
        denom = sum((t - mean_t) ** 2 for t, _ in data)
        if denom <= 0:
            return f"{target}%: collecting"
        slope = sum((t - mean_t) * (v - mean_v) for t, v in data) / denom
        if not math.isfinite(slope) or abs(slope) < 1e-9:
            return f"{target}%: flat"

        seconds = (target - current) / slope
        if not math.isfinite(seconds) or seconds <= 0:
            return f"{target}%: moving away"
        return f"{target}% in {_format_duration(seconds)}"


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h" + (f" {minutes}m" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days}d" + (f" {hours}h" if hours else "")


@dataclass
class GridCell:
    """One numeric readout inside a grid tile."""

    title: str
    source: str
    metric: str | None = None
    chart: str | None = None
    dimension: str | None = None
    entity: str | None = None
    attribute: str | None = None
    appid: int | None = None
    label: str | None = None
    unit: str | None = None
    scale: float = 1.0
    secondary: str | None = None
    decimals: int = 2
    # Runtime state, not from JSON.
    last_fields: dict[str, Any] | None = field(default=None, repr=False)


@dataclass
class TileConfig:
    index: int
    title: str
    source: str
    metric: str | None = None
    chart: str | None = None
    dimension: str | None = None
    entity: str | None = None
    attribute: str | None = None
    appid: int | None = None
    label: str | None = None
    unit: str | None = None
    scale: float = 1.0
    secondary: str | None = None
    warn: float | None = None
    crit: float | None = None
    eta: int | None = None
    subtexts: list[SubtextSpec | None] | None = None
    # Per-tile timing, resolved against the global defaults in load_config().
    poll_seconds: float = 0.0
    graph_length: int = 0
    time_unit: str | None = None
    graph_length_explicit: bool = False
    # Graph-only display options introduced in Polymath 1.5.
    start_at_zero: bool = True
    initial_values: list[float] | None = None
    # Grid-only cells, in the order Polymath draws grid1..grid6.
    cells: list[GridCell] = field(default_factory=list)
    # Runtime state, not from JSON.
    id: str = ""
    last_fields: dict[str, Any] | None = None
    last_cells: list[dict[str, str]] | None = None
    last_ok: float | None = None
    last_error: str | None = None
    applied_graph_length: int | None = None
    applied_tile_type: str | None = None
    next_poll: float = 0.0


def derive_time_unit(poll_seconds: float) -> str:
    """The x-axis label for a tile polled every ``poll_seconds`` seconds."""
    return "s" if poll_seconds == 1 else f"× {poll_seconds:g} s"


@dataclass
class ServerInfo:
    name: str = ""
    cores: int | None = None
    ram_total_mib: float | None = None


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ConfigError(f"{label} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ConfigError(f"{label} must be finite, got {value!r}")
    return number


def _integer(value: Any, label: str) -> int:
    number = _number(value, label)
    if not number.is_integer():
        raise ConfigError(f"{label} must be an integer, got {value!r}")
    return int(number)


def _require_number(raw: dict[str, Any], key: str, default: float | int) -> float:
    return _number(raw.get(key, default), repr(key))


def _tile_number(item: dict[str, Any], key: str, position: int, default: float | int) -> float:
    return _number(item.get(key, default), f"tiles[{position}].{key}")


def _one_line(value: Any, label: str) -> str:
    text = str(value)
    if any(ch in text for ch in "\r\n\t"):
        raise ConfigError(f"{label} must be one line")
    return text


def ssl_context_for(base_url: str, insecure: bool = False, ca: str | None = None) -> ssl.SSLContext | None:
    """TLS context for an HTTPS source, or None for plain HTTP."""
    if not base_url.startswith("https://"):
        return None
    if insecure:
        return ssl._create_unverified_context()
    if ca:
        return ssl.create_default_context(cafile=str(expand_path(ca)))
    return ssl.create_default_context()


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


def _parse_eta(value: Any, label: str) -> int:
    target = _integer(value, label)
    if target not in ETA_TARGETS:
        raise ConfigError(f"{label} must be 0 or 100")
    return target


def _parse_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be true or false")
    return value


def _parse_initial_values(value: Any, label: str) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be an array of numbers")
    if len(value) > 100000:
        raise ConfigError(f"{label} supports at most 100000 values")
    return [_number(item, f"{label}[{position}]") for position, item in enumerate(value)]


def _parse_grid_cells(
    raw: Any, position: int, cfg: dict[str, Any]
) -> list[GridCell]:
    label = f"tiles[{position}].cells"
    if not isinstance(raw, list):
        raise ConfigError(f"{label} must be an array")
    if not 1 <= len(raw) <= GRID_MAX_CELLS:
        raise ConfigError(f"{label} must contain between 1 and {GRID_MAX_CELLS} cells")

    cells: list[GridCell] = []
    seen_titles: set[str] = set()
    for slot, item in enumerate(raw):
        cell_label = f"{label}[{slot}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{cell_label} must be an object")
        extra = sorted(set(item) - GRID_CELL_KEYS)
        if extra:
            raise ConfigError(f"{cell_label} has unknown setting(s): {', '.join(extra)}")
        try:
            raw_title = item["title"]
            raw_source = item["source"]
        except KeyError as exc:
            raise ConfigError(f"{cell_label} is missing {exc.args[0]!r}") from None

        title = _one_line(raw_title, f"{cell_label}.title").strip()
        if not title:
            raise ConfigError(f"{cell_label}.title must not be empty")
        if title in seen_titles:
            raise ConfigError(f"{cell_label}.title duplicates another grid cell title")
        source = str(raw_source).strip().lower()
        if source not in DATA_SOURCES:
            raise ConfigError(
                f"{cell_label}.source must be one of {', '.join(sorted(DATA_SOURCES))}"
            )

        metric: str | None = None
        if source == "netdata":
            if item.get("metric") is None:
                raise ConfigError(
                    f"{cell_label}.source='netdata' needs a 'metric' "
                    f"({', '.join(sorted(NETDATA_METRICS))})"
                )
            metric = str(item.get("metric")).strip().lower()
            if metric not in NETDATA_METRICS:
                raise ConfigError(
                    f"{cell_label}.metric must be one of "
                    f"{', '.join(sorted(NETDATA_METRICS))}"
                )
        elif item.get("metric") is not None:
            raise ConfigError(
                f"{cell_label}: 'metric' is only valid with source='netdata'"
            )

        chart = (
            _one_line(item.get("chart"), f"{cell_label}.chart").strip()
            if item.get("chart") is not None
            else None
        )
        if chart is not None and (
            source != "netdata" or metric not in {"gpu", "disk", "temp", "custom"}
        ):
            raise ConfigError(
                f"{cell_label}: 'chart' is only valid for Netdata "
                "gpu, disk, temp, and custom metrics"
            )
        if metric in {"disk", "temp", "custom"} and not chart:
            raise ConfigError(
                f"{cell_label} source='netdata' metric={metric!r} needs a 'chart'"
            )

        dimension = (
            _one_line(item.get("dimension"), f"{cell_label}.dimension").strip()
            if item.get("dimension") is not None
            else None
        )
        if dimension is not None and (
            source != "netdata" or metric not in {"gpu", "temp", "custom"}
        ):
            raise ConfigError(
                f"{cell_label}: 'dimension' is only valid for Netdata "
                "gpu, temp, and custom metrics"
            )

        entity = (
            _one_line(item.get("entity"), f"{cell_label}.entity").strip()
            if item.get("entity") is not None
            else None
        )
        attribute = (
            _one_line(item.get("attribute"), f"{cell_label}.attribute").strip()
            if item.get("attribute") is not None
            else None
        )
        if source == "hass":
            if not entity:
                raise ConfigError(f"{cell_label} source='hass' needs an 'entity'")
            if "." not in entity:
                raise ConfigError(
                    f"{cell_label}.entity must be a full entity id, e.g. sensor.example"
                )
        elif entity is not None or attribute is not None:
            raise ConfigError(
                f"{cell_label}: 'entity'/'attribute' are only valid with source='hass'"
            )

        appid: int | None = None
        if item.get("appid") is not None:
            appid = _integer(item.get("appid"), f"{cell_label}.appid")
            if appid <= 0:
                raise ConfigError(f"{cell_label}.appid must be a positive integer")
        if source == "steam":
            if appid is None:
                raise ConfigError(f"{cell_label} source='steam' needs an 'appid'")
        elif appid is not None:
            raise ConfigError(
                f"{cell_label}: 'appid' is only valid with source='steam'"
            )

        if source == "plex" and not cfg["plex_token"]:
            raise ConfigError(
                f"{cell_label} source='plex' needs a 'plex_token' "
                "(or the PLEX_TOKEN environment variable)"
            )

        label_name = (
            _one_line(item.get("label"), f"{cell_label}.label").strip()
            if item.get("label") is not None
            else None
        )
        if label_name is not None and not (source == "netdata" and metric == "disk"):
            raise ConfigError(
                f"{cell_label}: 'label' is only valid for Netdata disk metrics"
            )
        unit = (
            _one_line(item.get("unit"), f"{cell_label}.unit")
            if item.get("unit") is not None
            else None
        )
        secondary = (
            _one_line(item.get("secondary"), f"{cell_label}.secondary")
            if item.get("secondary") is not None
            else None
        )
        decimals = _integer(item.get("decimals", 2), f"{cell_label}.decimals")
        if not 0 <= decimals <= 6:
            raise ConfigError(f"{cell_label}.decimals must be between 0 and 6")
        scale = _number(item.get("scale", 1.0), f"{cell_label}.scale")

        seen_titles.add(title)
        cells.append(
            GridCell(
                title=title,
                source=source,
                metric=metric,
                chart=chart,
                dimension=dimension,
                entity=entity,
                attribute=attribute,
                appid=appid,
                label=label_name,
                unit=unit,
                scale=scale,
                secondary=secondary,
                decimals=decimals,
            )
        )
    return cells


def _subtext_line(value: Any, label: str) -> str | None:
    if value is None:
        return None
    text = _one_line(value, label).strip()
    return text or None


def _parse_subtexts(
    raw: Any,
    position: int,
    *,
    source: str,
    entity: str | None,
    attribute: str | None,
    appid: int | None,
) -> list[SubtextSpec | None] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ConfigError(f"tiles[{position}].subtexts must be an array")
    if len(raw) > 2:
        raise ConfigError(f"tiles[{position}].subtexts supports at most two items")
    if not raw:
        return []

    specs: list[SubtextSpec | None] = []
    for slot, entry in enumerate(raw):
        label = f"tiles[{position}].subtexts[{slot}]"
        if entry is None:
            specs.append(None)
            continue
        if isinstance(entry, str):
            text = _one_line(entry, label).strip()
            if not text:
                raise ConfigError(f"{label} must not be empty")
            specs.append(SubtextSpec(source="text", slot=slot, text=text))
            continue
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be an object, a string, or null")

        source = str(entry.get("source") or "").strip().lower()
        if source not in SUBTEXT_SOURCES:
            raise ConfigError(
                f"{label}.source must be one of {', '.join(sorted(SUBTEXT_SOURCES))}"
            )
        allowed = SUBTEXT_COMMON_KEYS | SUBTEXT_SOURCE_KEYS[source]
        extra = sorted(set(entry) - allowed)
        if extra:
            raise ConfigError(f"{label} has unknown setting(s): {', '.join(extra)}")

        spec = SubtextSpec(source=source, slot=slot)
        if source == "text":
            if set(entry) - {"source", "text"}:
                raise ConfigError(f"{label}: only 'text' is valid with source='text'")
            spec.text = _subtext_line(entry.get("text"), f"{label}.text")
            if not spec.text:
                raise ConfigError(f"{label} source='text' needs a 'text'")
            specs.append(spec)
            continue

        spec.label = _subtext_line(entry.get("label"), f"{label}.label")
        spec.unit = _subtext_line(entry.get("unit"), f"{label}.unit")
        spec.scale = _number(entry.get("scale", 1.0), f"{label}.scale")
        if entry.get("decimals") is not None:
            spec.decimals = _integer(entry.get("decimals"), f"{label}.decimals")
            if not 0 <= spec.decimals <= 6:
                raise ConfigError(f"{label}.decimals must be between 0 and 6")
        if entry.get("eta") is not None:
            spec.eta = _parse_eta(entry.get("eta"), f"{label}.eta")
            if spec.unit is not None and spec.unit != "%":
                raise ConfigError(f"{label}.eta needs a percentage unit")

        if source == "netdata":
            spec.chart = _subtext_line(entry.get("chart"), f"{label}.chart")
            if not spec.chart:
                raise ConfigError(f"{label} source='netdata' needs a 'chart'")
            spec.dimension = _subtext_line(
                entry.get("dimension"), f"{label}.dimension"
            )
        elif source == "hass":
            inherited = "entity" not in entry
            spec.entity = (
                _subtext_line(entry.get("entity"), f"{label}.entity")
                or (entity if inherited else None)
            )
            if not spec.entity:
                raise ConfigError(f"{label} source='hass' needs an 'entity'")
            if "." not in spec.entity:
                raise ConfigError(
                    f"{label}.entity must be a full entity id, e.g. sensor.example"
                )
            spec.attribute = (
                _subtext_line(entry.get("attribute"), f"{label}.attribute")
                or (attribute if inherited else None)
            )
        elif source == "llama":
            spec.field = str(entry.get("field") or "total").strip().lower()
            if spec.field not in LLAMA_FIELDS:
                raise ConfigError(
                    f"{label}.field must be one of {', '.join(sorted(LLAMA_FIELDS))}"
                )
        elif source == "steam":
            if entry.get("appid") is not None:
                spec.appid = _integer(entry.get("appid"), f"{label}.appid")
                if spec.appid <= 0:
                    raise ConfigError(f"{label}.appid must be a positive integer")
            else:
                spec.appid = appid
            if spec.appid is None:
                raise ConfigError(f"{label} source='steam' needs an 'appid'")
        elif source == "plex":
            spec.field = str(entry.get("field") or "total").strip().lower()
            if spec.field not in PLEX_FIELDS:
                raise ConfigError(
                    f"{label}.field must be one of {', '.join(sorted(PLEX_FIELDS))}"
                )
        specs.append(spec)
    return specs


def load_config(path: Path, *, validate_tiles: bool = True) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from None

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

    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"Unknown config setting(s): {', '.join(unknown)}")

    cfg: dict[str, Any] = dict(DEFAULTS)
    cfg.update(raw)

    url = _one_line(cfg["netdata_url"], "netdata_url").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("netdata_url must start with http:// or https://")
    cfg["netdata_url"] = url

    cfg["poll_seconds"] = _require_number(cfg, "poll_seconds", 2.0)
    if cfg["poll_seconds"] < 1.0:
        raise ConfigError("poll_seconds must be at least 1.0")
    cfg["netdata_timeout"] = _require_number(cfg, "netdata_timeout", 5.0)
    if cfg["netdata_timeout"] <= 0:
        raise ConfigError("netdata_timeout must be positive")
    if not isinstance(cfg["netdata_insecure"], bool):
        raise ConfigError("netdata_insecure must be true or false")
    cfg["netdata_ca"] = (
        _one_line(cfg["netdata_ca"], "netdata_ca").strip() or None
        if cfg.get("netdata_ca") is not None
        else None
    )

    cfg["server_name"] = _one_line(cfg.get("server_name") or "", "server_name").strip()
    cfg["user_agent"] = (
        _one_line(cfg.get("user_agent") or APP_NAME, "user_agent").strip() or APP_NAME
    )

    llama_url = _one_line(cfg["llama_url"], "llama_url").strip().rstrip("/")
    if not llama_url.startswith(("http://", "https://")):
        raise ConfigError("llama_url must start with http:// or https://")
    cfg["llama_url"] = llama_url
    cfg["llama_api_key"] = _one_line(cfg.get("llama_api_key") or "", "llama_api_key").strip()
    cfg["llama_timeout"] = _require_number(cfg, "llama_timeout", 5.0)
    if cfg["llama_timeout"] <= 0:
        raise ConfigError("llama_timeout must be positive")
    cfg["llama_model_ttl"] = _require_number(cfg, "llama_model_ttl", 0.0)
    if cfg["llama_model_ttl"] < 0:
        raise ConfigError("llama_model_ttl must be zero or positive")
    if not isinstance(cfg["llama_insecure"], bool):
        raise ConfigError("llama_insecure must be true or false")
    cfg["llama_ca"] = (
        _one_line(cfg["llama_ca"], "llama_ca").strip() or None
        if cfg.get("llama_ca") is not None
        else None
    )

    hass_url = _one_line(cfg["hass_url"], "hass_url").strip().rstrip("/")
    if not hass_url.startswith(("http://", "https://")):
        raise ConfigError("hass_url must start with http:// or https://")
    cfg["hass_url"] = hass_url
    cfg["hass_token"] = _one_line(
        cfg.get("hass_token") or os.environ.get("HASS_TOKEN", ""), "hass_token"
    ).strip()
    cfg["hass_timeout"] = _require_number(cfg, "hass_timeout", 5.0)
    if cfg["hass_timeout"] <= 0:
        raise ConfigError("hass_timeout must be positive")
    if not isinstance(cfg["hass_insecure"], bool):
        raise ConfigError("hass_insecure must be true or false")
    cfg["hass_ca"] = (
        _one_line(cfg["hass_ca"], "hass_ca").strip() or None
        if cfg.get("hass_ca") is not None
        else None
    )

    steam_url = _one_line(cfg["steam_url"], "steam_url").strip().rstrip("/")
    if not steam_url.startswith(("http://", "https://")):
        raise ConfigError("steam_url must start with http:// or https://")
    cfg["steam_url"] = steam_url
    cfg["steam_timeout"] = _require_number(cfg, "steam_timeout", 5.0)
    if cfg["steam_timeout"] <= 0:
        raise ConfigError("steam_timeout must be positive")
    if not isinstance(cfg["steam_insecure"], bool):
        raise ConfigError("steam_insecure must be true or false")
    cfg["steam_ca"] = (
        _one_line(cfg["steam_ca"], "steam_ca").strip() or None
        if cfg.get("steam_ca") is not None
        else None
    )

    plex_url = _one_line(cfg["plex_url"], "plex_url").strip().rstrip("/")
    if not plex_url.startswith(("http://", "https://")):
        raise ConfigError("plex_url must start with http:// or https://")
    cfg["plex_url"] = plex_url
    cfg["plex_token"] = _one_line(
        cfg.get("plex_token") or os.environ.get("PLEX_TOKEN", ""), "plex_token"
    ).strip()
    cfg["plex_timeout"] = _require_number(cfg, "plex_timeout", 5.0)
    if cfg["plex_timeout"] <= 0:
        raise ConfigError("plex_timeout must be positive")
    if not isinstance(cfg["plex_insecure"], bool):
        raise ConfigError("plex_insecure must be true or false")
    cfg["plex_ca"] = (
        _one_line(cfg["plex_ca"], "plex_ca").strip() or None
        if cfg.get("plex_ca") is not None
        else None
    )

    host = _one_line(cfg.get("polymath_host") or "localhost", "polymath_host").strip()
    if not host or "/" in host:
        raise ConfigError("polymath_host must be a hostname or IP address")
    cfg["polymath_host"] = host

    cfg["graph_length"] = _integer(cfg.get("graph_length", 120), "graph_length")
    if not 1 <= cfg["graph_length"] <= 100000:
        raise ConfigError("graph_length must be between 1 and 100000")

    time_unit = cfg.get("time_unit")
    if time_unit is not None:
        time_unit = _one_line(time_unit, "time_unit").strip()
        cfg["time_unit"] = time_unit or None

    if not validate_tiles:
        cfg["tiles"] = []
        return cfg

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
        extra = sorted(set(item) - TILE_KEYS)
        if extra:
            raise ConfigError(f"tiles[{position}] has unknown setting(s): {', '.join(extra)}")
        try:
            raw_index = item["index"]
            raw_title = item["title"]
            raw_source = item["source"]
        except KeyError as exc:
            raise ConfigError(f"tiles[{position}] is missing {exc.args[0]!r}") from None

        index = _integer(raw_index, f"tiles[{position}].index")
        title = _one_line(raw_title, f"tiles[{position}].title").strip()
        source = str(raw_source).strip().lower()

        if not 0 <= index <= 3:
            raise ConfigError(f"tiles[{position}].index must be between 0 and 3")
        if index in seen_indexes:
            raise ConfigError(f"Duplicate tile index {index}")
        if not title:
            raise ConfigError(f"tiles[{position}].title must not be empty")
        if title in seen_titles:
            raise ConfigError(f"Duplicate tile title {title!r}; the title is the tile ID")
        if source not in TILE_SOURCES:
            raise ConfigError(
                f"tiles[{position}].source must be one of {', '.join(sorted(TILE_SOURCES))}"
            )

        if source == "grid":
            # A grid is a layout, not a data source: it has cells, no graph
            # length, and no single value/unit/subtext.
            allowed = {"index", "title", "source", "cells", "poll_seconds"}
            extra = sorted(set(item) - allowed)
            if extra:
                raise ConfigError(
                    f"tiles[{position}]: {', '.join(extra)} "
                    "is not valid with source='grid'"
                )
            cells = _parse_grid_cells(item.get("cells"), position, cfg)
            if item.get("poll_seconds") is not None:
                tile_poll = _tile_number(
                    item, "poll_seconds", position, cfg["poll_seconds"]
                )
                if tile_poll < 1.0:
                    raise ConfigError(
                        f"tiles[{position}].poll_seconds must be at least 1.0"
                    )
            else:
                tile_poll = float(cfg["poll_seconds"])
            seen_indexes.add(index)
            seen_titles.add(title)
            tiles.append(
                TileConfig(
                    index=index,
                    title=title,
                    source="grid",
                    cells=cells,
                    poll_seconds=tile_poll,
                    graph_length=int(cfg["graph_length"]),
                    time_unit=derive_time_unit(tile_poll),
                )
            )
            continue

        if item.get("cells") is not None:
            raise ConfigError(
                f"tiles[{position}]: 'cells' is only valid with source='grid'"
            )

        metric: str | None = None
        if source == "netdata":
            if item.get("metric") is None:
                raise ConfigError(
                    f"tiles[{position}].source='netdata' needs a 'metric' "
                    f"({', '.join(sorted(NETDATA_METRICS))})"
                )
            metric = str(item.get("metric")).strip().lower()
            if metric not in NETDATA_METRICS:
                raise ConfigError(
                    f"tiles[{position}].metric must be one of "
                    f"{', '.join(sorted(NETDATA_METRICS))}"
                )
        elif item.get("metric") is not None:
            raise ConfigError(
                f"tiles[{position}]: 'metric' is only valid with source='netdata'"
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
            tile_graph_length = _integer(
                item.get("graph_length"), f"tiles[{position}].graph_length"
            )
            if not 1 <= tile_graph_length <= 100000:
                raise ConfigError(
                    f"tiles[{position}].graph_length must be between 1 and 100000"
                )
        else:
            tile_graph_length = int(cfg["graph_length"])

        if "time_unit" in item:
            raw_unit = item.get("time_unit")
            tile_time_unit = (
                _one_line(raw_unit, f"tiles[{position}].time_unit").strip()
                if raw_unit is not None
                else ""
            )
            tile_time_unit = tile_time_unit or derive_time_unit(tile_poll)
        elif cfg.get("time_unit") and tile_poll == float(cfg["poll_seconds"]):
            # An explicit global label still applies to tiles that keep the
            # global interval; tiles polled at a different rate get a label
            # that matches their own x-axis.
            tile_time_unit = str(cfg["time_unit"])
        else:
            tile_time_unit = derive_time_unit(tile_poll)

        chart = (
            _one_line(item.get("chart"), f"tiles[{position}].chart").strip()
            if item.get("chart") is not None
            else None
        )
        if chart is not None and (
            source != "netdata" or metric not in {"gpu", "disk", "temp", "custom"}
        ):
            raise ConfigError(
                f"tiles[{position}]: 'chart' is only valid for Netdata "
                "gpu, disk, temp, and custom metrics"
            )
        if metric in {"disk", "temp", "custom"} and not chart:
            raise ConfigError(
                f"tiles[{position}] source='netdata' metric={metric!r} needs a 'chart'"
            )

        dimension = (
            _one_line(item.get("dimension"), f"tiles[{position}].dimension").strip()
            if item.get("dimension") is not None
            else None
        )
        if dimension is not None and (
            source != "netdata" or metric not in {"gpu", "temp", "custom"}
        ):
            raise ConfigError(
                f"tiles[{position}]: 'dimension' is only valid for Netdata "
                "gpu, temp, and custom metrics"
            )

        entity = (
            _one_line(item.get("entity"), f"tiles[{position}].entity").strip()
            if item.get("entity") is not None
            else None
        )
        attribute = (
            _one_line(item.get("attribute"), f"tiles[{position}].attribute").strip()
            if item.get("attribute") is not None
            else None
        )
        if source == "hass":
            if not entity:
                raise ConfigError(f"tiles[{position}] source='hass' needs an 'entity'")
            if "." not in entity:
                raise ConfigError(
                    f"tiles[{position}].entity must be a full entity id, e.g. sensor.example"
                )
        elif entity is not None or attribute is not None:
            raise ConfigError(
                f"tiles[{position}]: 'entity'/'attribute' are only valid with source='hass'"
            )

        appid: int | None = None
        if item.get("appid") is not None:
            appid = _integer(item.get("appid"), f"tiles[{position}].appid")
            if appid <= 0:
                raise ConfigError(f"tiles[{position}].appid must be a positive integer")
        if source == "steam":
            if appid is None:
                raise ConfigError(f"tiles[{position}] source='steam' needs an 'appid'")
        elif appid is not None:
            raise ConfigError(
                f"tiles[{position}]: 'appid' is only valid with source='steam'"
            )

        if source == "plex" and not cfg["plex_token"]:
            raise ConfigError(
                f"tiles[{position}] source='plex' needs a 'plex_token' "
                "(or the PLEX_TOKEN environment variable)"
            )

        label = (
            _one_line(item.get("label"), f"tiles[{position}].label").strip()
            if item.get("label") is not None
            else None
        )
        if label is not None and not (source == "netdata" and metric == "disk"):
            raise ConfigError(
                f"tiles[{position}]: 'label' is only valid for Netdata disk metrics"
            )
        unit = (
            _one_line(item.get("unit"), f"tiles[{position}].unit")
            if item.get("unit") is not None
            else None
        )
        secondary = (
            _one_line(item.get("secondary"), f"tiles[{position}].secondary")
            if item.get("secondary") is not None
            else None
        )
        start_at_zero = (
            _parse_bool(
                item.get("start_at_zero"), f"tiles[{position}].start_at_zero"
            )
            if item.get("start_at_zero") is not None
            else True
        )
        initial_values = _parse_initial_values(
            item.get("initial_values"), f"tiles[{position}].initial_values"
        )
        if initial_values is not None and len(initial_values) > tile_graph_length:
            raise ConfigError(
                f"tiles[{position}].initial_values has {len(initial_values)} values, "
                f"more than graph_length ({tile_graph_length})"
            )

        eta = (
            _parse_eta(item.get("eta"), f"tiles[{position}].eta")
            if item.get("eta") is not None
            else None
        )
        if eta is not None:
            if source != "hass":
                raise ConfigError(f"tiles[{position}].eta is only valid with source='hass'")
            if unit is not None and unit.strip() != "%":
                raise ConfigError(f"tiles[{position}].eta needs a percentage unit")
        subtexts = _parse_subtexts(
            item.get("subtexts"),
            position,
            source=source,
            entity=entity,
            attribute=attribute,
            appid=appid,
        )
        if any(spec is not None and spec.source == "plex" for spec in (subtexts or [])):
            if not cfg["plex_token"]:
                raise ConfigError(
                    f"tiles[{position}].subtexts source='plex' needs a 'plex_token' "
                    "(or the PLEX_TOKEN environment variable)"
                )

        scale = _number(item.get("scale", 1.0), f"tiles[{position}].scale")
        warn = (
            _number(item.get("warn"), f"tiles[{position}].warn")
            if item.get("warn") is not None
            else None
        )
        crit = (
            _number(item.get("crit"), f"tiles[{position}].crit")
            if item.get("crit") is not None
            else None
        )
        if (warn is None) != (crit is None):
            raise ConfigError(f"tiles[{position}].warn and crit must be set together")
        if warn is not None and crit is not None and warn > crit:
            raise ConfigError(f"tiles[{position}].warn must not exceed crit")

        seen_indexes.add(index)
        seen_titles.add(title)
        tiles.append(
            TileConfig(
                index=index,
                title=title,
                source=source,
                metric=metric,
                chart=chart,
                dimension=dimension,
                entity=entity,
                attribute=attribute,
                appid=appid,
                label=label,
                unit=unit,
                scale=scale,
                secondary=secondary,
                warn=warn,
                crit=crit,
                eta=eta,
                subtexts=subtexts,
                poll_seconds=tile_poll,
                graph_length=tile_graph_length,
                time_unit=tile_time_unit,
                graph_length_explicit=graph_length_explicit,
                start_at_zero=start_at_zero,
                initial_values=initial_values,
            )
        )

    cfg["tiles"] = sorted(tiles, key=lambda t: t.index)
    return cfg


class NetdataClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        insecure: bool = False,
        ca: str | None = None,
        user_agent: str = APP_NAME,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.insecure = insecure
        self.ca = expand_path(ca) if ca else None
        self.user_agent = user_agent
        self._ssl_context: ssl.SSLContext | None = None

    def _context(self) -> ssl.SSLContext | None:
        if self._ssl_context is None:
            self._ssl_context = ssl_context_for(self.base_url, self.insecure, self.ca)
        return self._ssl_context

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": f"{self.user_agent}/{VERSION}"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._context()
            ) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise NetdataError(
                f"Netdata HTTP {exc.code} for {url}: {compact(body, 200)}", status=exc.code
            ) from None
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

    The router exposes /v1/models and needs ?model=<id> on /metrics and
    /slots.  Only the loaded model is queried; asking for an unloaded model
    can make the router autoload it (and evict the current one).

    The /metrics rate gauges only see data when a request finishes: llama.cpp
    flushes a slot's generation stats into the bucket when the slot is
    released, so during a generation both the gauges and the cumulative
    counters are frozen, and one completion spike is all you get.  To graph
    live throughput, this client samples /slots (whose per-slot
    ``next_token[].n_decoded`` and ``n_prompt_tokens_processed`` advance in
    real time) and differentiates the counters between polls, keyed by task id
    so a new request never produces a bogus jump.  Every slot in the response
    is included; llama.cpp may expose any number of slots.
    """

    # Short shared caches keep multiple llama tiles and repeated polls from
    # making the same /v1/models, /metrics, and /slots requests over and over.
    CACHE_TTL = 0.5

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 5.0,
        model_ttl: float = 10.0,
        insecure: bool = False,
        ca: str | None = None,
        user_agent: str = APP_NAME,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.model_ttl = model_ttl
        self.insecure = insecure
        self.ca = expand_path(ca) if ca else None
        self.user_agent = user_agent
        self._ssl_context: ssl.SSLContext | None = None
        self._model: str | None = None
        self._model_checked = 0.0
        self._slots_supported = True
        self._slots_warned = 0.0
        self._prev_slots: dict[int, tuple[Any, float, float, float]] = {}
        self._last_slot_poll = 0.0
        self._rates_cache: dict[str, float] | None = None
        self._rates_cache_t = 0.0
        self._rates_cache_model = ""
        self._rates_cache_ready = False
        self._metrics_cache: dict[str, tuple[float, dict[str, float]]] = {}

    def _context(self) -> ssl.SSLContext | None:
        if self._ssl_context is None:
            self._ssl_context = ssl_context_for(self.base_url, self.insecure, self.ca)
        return self._ssl_context

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": f"{self.user_agent}/{VERSION}"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._context()
            ) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 401:
                raise LlamaError("llama.cpp rejected the API key (HTTP 401)", status=401) from None
            if exc.code == 403:
                raise LlamaError("llama.cpp denied the request (HTTP 403)", status=403) from None
            raise LlamaError(
                f"llama.cpp HTTP {exc.code} for {url}: {compact(body, 180)}", status=exc.code
            ) from None
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
        cache_ttl = max(self.model_ttl, self.CACHE_TTL)
        if self._model_checked and now - self._model_checked < cache_ttl:
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
            status = data[0].get("status")
            state = status.get("value") if isinstance(status, dict) else status
            if only and state in (None, "", "loaded"):
                loaded.append(only)

        self._model = loaded[0] if loaded else None
        self._model_checked = now
        return self._model

    def metrics(self, model: str) -> dict[str, float]:
        now = time.monotonic()
        cached = self._metrics_cache.get(model)
        if cached is not None and now - cached[0] < self.CACHE_TTL:
            return dict(cached[1])

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
                value = float(parts[-1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values[name] = value
        if not values:
            raise LlamaError("llama.cpp /metrics returned no numeric metrics")
        self._metrics_cache[model] = (now, dict(values))
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
            if exc.status in (404, 405):
                self._slots_supported = False
                LOG.info("llama.cpp has no /slots endpoint; using /metrics only")
            else:
                now = time.monotonic()
                if now - self._slots_warned >= 30.0:
                    self._slots_warned = now
                    LOG.warning("llama.cpp /slots unavailable this poll (retrying): %s", compact(str(exc), 140))
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
        """One differentiation pass over every slot: (pp, tg, current, processing).

        ``n_prompt_tokens_processed`` and ``n_decoded`` advance in real time
        and restart at 0 for a new task, so a task seen for the first time
        baselines at 0 over the whole window.  Negative jumps (new task,
        reset slot) clamp to 0 and never leak across requests.  The response
        can contain any number of slots; all processing slots are summed.
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
                            value = float(item.get("n_decoded") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        if math.isfinite(value):
                            decoded += value

            try:
                prompt_done = float(slot.get("n_prompt_tokens_processed") or 0.0)
            except (TypeError, ValueError):
                prompt_done = 0.0
            if not math.isfinite(prompt_done):
                prompt_done = 0.0

            task_id = slot.get("id_task")
            current[slot_id] = (task_id, decoded, prompt_done, now)

            is_processing = slot.get("is_processing")
            if isinstance(is_processing, str):
                is_processing = is_processing.strip().lower() not in ("", "0", "false", "no")
            if not is_processing:
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
        if (
            self._rates_cache_model == model
            and self._rates_cache_ready
            and now - self._rates_cache_t < self.CACHE_TTL
        ):
            return self._rates_cache
        if self._rates_cache_model and self._rates_cache_model != model:
            self._prev_slots = {}
            self._last_slot_poll = 0.0
            self._rates_cache_ready = False

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
        self._rates_cache_model = model
        self._rates_cache_ready = True
        return result


class HassClient:
    """Minimal read-only client for the Home Assistant REST API.

    Uses a long-lived access token and only reads entity states
    (GET /api/states/...); it never calls services.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 5.0,
        insecure: bool = False,
        ca: str | None = None,
        user_agent: str = APP_NAME,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        self.insecure = insecure
        self.ca = expand_path(ca) if ca else None
        self.user_agent = user_agent
        self._ssl_context: ssl.SSLContext | None = None

    def _context(self) -> ssl.SSLContext | None:
        if self._ssl_context is None:
            self._ssl_context = ssl_context_for(self.base_url, self.insecure, self.ca)
        return self._ssl_context

    def _get_json(self, path: str) -> Any:
        if not self.token:
            raise HassError(
                "No Home Assistant token: set hass_token in the config or the HASS_TOKEN env var"
            )
        url = self.base_url + path
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "User-Agent": f"{self.user_agent}/{VERSION}",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._context()
            ) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise HassError(
                    f"Home Assistant rejected the token (HTTP {exc.code})", status=exc.code
                ) from None
            raise HassError(
                f"Home Assistant HTTP {exc.code} for {url}: {compact(body, 180)}", status=exc.code
            ) from None
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
            if exc.status == 404:
                raise HassError(f"Home Assistant has no entity {entity_id!r}", status=404) from None
            raise
        if not isinstance(payload, dict) or "state" not in payload:
            raise HassError(f"Home Assistant returned an unexpected payload for {entity_id!r}")
        return payload

    def states(self) -> list[dict[str, Any]]:
        payload = self._get_json("/api/states")
        if not isinstance(payload, list):
            raise HassError("Home Assistant /api/states returned an unexpected payload")
        return payload


class SteamClient:
    """Minimal client for Steam's public current-player-count endpoint.

    ``GetNumberOfCurrentPlayers`` is keyless and identifies the game by its
    numeric Steam application id (the number in a store URL).  Steam returns
    ``result: 42`` for an appid that has no player statistics.
    """

    PATH = "/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        insecure: bool = False,
        ca: str | None = None,
        user_agent: str = APP_NAME,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.insecure = insecure
        self.ca = expand_path(ca) if ca else None
        self.user_agent = user_agent
        self._ssl_context: ssl.SSLContext | None = None

    def _context(self) -> ssl.SSLContext | None:
        if self._ssl_context is None:
            self._ssl_context = ssl_context_for(self.base_url, self.insecure, self.ca)
        return self._ssl_context

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": f"{self.user_agent}/{VERSION}"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._context()
            ) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise SteamError(
                    f"Steam denied the request (HTTP {exc.code})", status=exc.code
                ) from None
            raise SteamError(
                f"Steam HTTP {exc.code} for {url}: {compact(body, 180)}", status=exc.code
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SteamError(f"Steam request failed for {url}: {exc}") from None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise SteamError(f"Steam returned non-JSON for {url}: {compact(raw, 120)}") from None

    def current_players(self, appid: int) -> int:
        try:
            payload = self._get_json(self.PATH, {"appid": appid})
        except SteamError as exc:
            if exc.status == 404:
                # Steam answers an appid without statistics with HTTP 404 and
                # a result:42 body, which is more useful as a tile message.
                raise SteamError(
                    f"Steam has no player count for appid {appid} (HTTP 404)",
                    status=404,
                ) from None
            raise
        response = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(response, dict):
            raise SteamError(f"Steam returned an unexpected payload for appid {appid}")

        result = response.get("result")
        if result is not None:
            try:
                result_code = int(result)
            except (TypeError, ValueError, OverflowError):
                result_code = None
            if result_code != 1:
                raise SteamError(
                    f"Steam has no player count for appid {appid} (result {result!r})"
                )

        raw_count = response.get("player_count")
        if isinstance(raw_count, bool) or not isinstance(raw_count, (int, float, str)):
            raise SteamError(f"Steam returned no player count for appid {appid}")
        try:
            count = float(raw_count)
        except (TypeError, ValueError, OverflowError):
            raise SteamError(f"Steam returned an invalid player count for appid {appid}") from None
        if not math.isfinite(count) or count < 0 or not count.is_integer():
            raise SteamError(f"Steam returned an invalid player count for appid {appid}")
        return int(count)


@dataclass
class PlexStatus:
    """A snapshot of Plex's active sessions."""

    total: int = 0
    transcoding: int = 0
    paused: int = 0

    @property
    def direct(self) -> int:
        return max(0, self.total - self.transcoding)


class PlexClient:
    """Minimal client for Plex Media Server's local API.

    ``/status/sessions`` requires the server owner's ``X-Plex-Token`` and is
    the authoritative list of open streams, including paused ones.  The token
    is sent as a header so it never appears in request URLs or error messages.
    """

    PATH = "/status/sessions"
    CLIENT_IDENTIFIER = "polymath-data-bridge"

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 5.0,
        insecure: bool = False,
        ca: str | None = None,
        user_agent: str = APP_NAME,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.insecure = insecure
        self.ca = expand_path(ca) if ca else None
        self.user_agent = user_agent
        self._ssl_context: ssl.SSLContext | None = None

    def _context(self) -> ssl.SSLContext | None:
        if self._ssl_context is None:
            self._ssl_context = ssl_context_for(self.base_url, self.insecure, self.ca)
        return self._ssl_context

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Plex-Token": self.token,
            "X-Plex-Product": self.user_agent,
            "X-Plex-Version": VERSION,
            "X-Plex-Client-Identifier": self.CLIENT_IDENTIFIER,
            "User-Agent": f"{self.user_agent}/{VERSION}",
        }

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            method="GET",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._context()
            ) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise PlexError(
                    f"Plex rejected the token (HTTP {exc.code})", status=exc.code
                ) from None
            raise PlexError(
                f"Plex HTTP {exc.code} for {path}: {compact(body, 180)}",
                status=exc.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PlexError(f"Plex request failed for {url}: {exc}") from None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise PlexError(
                f"Plex returned non-JSON for {path}: {compact(raw, 120)}"
            ) from None

    def sessions(self) -> PlexStatus:
        payload = self._get_json(self.PATH)
        container = payload.get("MediaContainer") if isinstance(payload, dict) else None
        if not isinstance(container, dict):
            raise PlexError("Plex /status/sessions returned an unexpected payload")

        raw_metadata = container.get("Metadata")
        if raw_metadata is None:
            items: list[Any] = []
        elif isinstance(raw_metadata, dict):
            items = [raw_metadata]
        elif isinstance(raw_metadata, list):
            items = raw_metadata
        else:
            raise PlexError("Plex /status/sessions returned invalid Metadata")
        if any(not isinstance(item, dict) for item in items):
            raise PlexError("Plex /status/sessions returned a non-object session")

        total = len(items)
        if not items:
            # With no Metadata, Plex still reports the number of sessions in
            # size.  Treat a malformed size as an error rather than as zero.
            raw_size = container.get("size", 0)
            if isinstance(raw_size, bool) or not isinstance(
                raw_size, (int, float, str)
            ):
                raise PlexError("Plex /status/sessions returned an invalid size")
            try:
                size = float(raw_size)
            except (TypeError, ValueError, OverflowError):
                raise PlexError(
                    "Plex /status/sessions returned an invalid size"
                ) from None
            if not math.isfinite(size) or size < 0 or not size.is_integer():
                raise PlexError("Plex /status/sessions returned an invalid size")
            total = int(size)

        transcoding = 0
        paused = 0
        for item in items:
            transcode_session = item.get("TranscodeSession")
            if isinstance(transcode_session, dict) and transcode_session:
                transcoding += 1
            player = item.get("Player")
            if (
                isinstance(player, dict)
                and str(player.get("state") or "").strip().lower() == "paused"
            ):
                paused += 1

        return PlexStatus(total=total, transcoding=transcoding, paused=paused)


def _hass_to_number(raw: Any, what: str) -> float:
    """Coerce an HA state or attribute to a finite float the graph can plot."""
    if raw is None:
        raise HassError(f"{what} has no value")
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        value = float(raw)
        if not math.isfinite(value):
            raise HassError(f"{what} is not finite: {raw!r}")
        return value
    text = str(raw).strip().lower()
    if text in ("", "unknown", "unavailable"):
        raise HassError(f"{what} is {text or 'empty'}")
    if text in ("on", "true", "yes"):
        return 1.0
    if text in ("off", "false", "no"):
        return 0.0
    try:
        value = float(text)
    except ValueError:
        raise HassError(f"{what} is not numeric: {compact(str(raw), 60)}") from None
    if not math.isfinite(value):
        raise HassError(f"{what} is not finite: {compact(str(raw), 60)}")
    return value


def _hass_reading(
    payload: dict[str, Any], entity: str, attribute: str | None
) -> tuple[Any, str, dict[str, Any]]:
    """Extract the configured state or attribute from an HA entity payload."""
    attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
    if attribute:
        if attribute not in attrs:
            raise HassError(f"{entity!r} has no attribute {attribute!r}")
        return attrs[attribute], f"{entity}.{attribute}", attrs
    return payload.get("state"), entity, attrs


def _server_info(nd: NetdataClient, fallback_name: str) -> ServerInfo:
    try:
        info = nd.info()
    except NetdataError as exc:
        LOG.warning("Could not read Netdata info: %s", exc)
        return ServerInfo(name=fallback_name, cores=None, ram_total_mib=None)

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

    ram_total_mib: float | None = None
    try:
        raw_ram = info.get("ram_total")
        if raw_ram is not None:
            ram_bytes = float(raw_ram)
            if math.isfinite(ram_bytes) and ram_bytes > 0:
                ram_total_mib = ram_bytes / (1024.0 * 1024.0)
    except (TypeError, ValueError, OverflowError):
        ram_total_mib = None
    return ServerInfo(name=name or fallback_name, cores=cores, ram_total_mib=ram_total_mib)


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
        # Some Netdata versions encode "/" as "_": disk_space._,
        # disk_space._var_log, and so on.
        if mount == "_":
            return "/"
        if mount.startswith("_"):
            return "/" + mount[1:].replace("_", "/")
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
    ram_total_mib: float | None = None
    # Optional so older callers that construct a context for non-Steam or
    # non-Plex tiles keep working; the bridge always supplies both.
    steam: SteamClient | None = None
    plex: PlexClient | None = None
    history: SeriesHistory = field(default_factory=SeriesHistory)


def _render_cpu(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    values = ctx.nd.sample("system.cpu")
    if "idle" in values:
        value = 100.0 - values["idle"]
    else:
        # Netdata's system.cpu chart is the total utilization chart and omits
        # idle.  guest/guest_nice are already included in user/nice on Linux,
        # so exclude them to avoid double-counting.
        value = sum(
            value for key, value in values.items() if key not in {"guest", "guest_nice"}
        )
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
    # Netdata's system.ram values are MiB.  Prefer the host total reported by
    # /api/v1/info so extra dimensions and cache accounting cannot skew it.
    total = ctx.ram_total_mib or (free + used + cached + buffers)
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
    # Netdata's default system.net chart is kilobits/s.
    down = abs(values.get("received", 0.0)) * tile.scale / 1000.0
    up = abs(values.get("sent", 0.0)) * tile.scale / 1000.0
    total_mbps = down + up
    unit = tile.unit or "Mbps"
    secondary = tile.secondary or f"↓ {down:.1f} · ↑ {up:.1f} {unit}"
    fields = _base_fields(ctx, tile, total_mbps, unit, secondary)
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
    try:
        values = ctx.llama.metrics(model)
    except LlamaError:
        if live is None:
            raise
        values = {}
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
    reserved = values.get("reserved for root")
    if reserved is None:
        reserved = values.get("reserved_for_root", 0.0)
    reserved = max(0.0, reserved)
    total = used + avail + reserved
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
        # Explicit thresholds are in displayed units (after scale).
        fields["color"] = health_colour(value, tile.warn, tile.crit)
    else:
        # Default thresholds are in the source's native (usually Celsius)
        # units, so compare before applying scale.
        fields["color"] = health_colour(raw, 70.0, 85.0)
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


def _render_steam(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    assert tile.appid is not None and ctx.steam is not None
    count = ctx.steam.current_players(tile.appid)
    value = float(count) * tile.scale
    unit = tile.unit or "players"
    secondary = tile.secondary or f"appid {tile.appid} · {count:,} playing"
    fields = _base_fields(ctx, tile, value, unit, secondary)
    fields["subtitle"] = "Steam"
    if tile.warn is not None and tile.crit is not None:
        fields["color"] = health_colour(value, tile.warn, tile.crit)
    else:
        fields["color"] = "Cyan"
    return fields


def _render_plex(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    assert ctx.plex is not None
    status = ctx.plex.sessions()
    value = float(status.total) * tile.scale
    unit = tile.unit or "streams"

    if tile.secondary:
        secondary = tile.secondary
    elif status.total == 0:
        secondary = "idle"
    else:
        parts = [f"{status.direct} direct", f"{status.transcoding} transcode"]
        if status.paused:
            parts.append(f"{status.paused} paused")
        secondary = " · ".join(parts)

    fields = _base_fields(ctx, tile, value, unit, secondary)
    fields["subtitle"] = "Plex"
    if tile.warn is not None and tile.crit is not None:
        fields["color"] = health_colour(value, tile.warn, tile.crit)
    else:
        fields["color"] = "Cyan" if status.total > 0 else "Legend"
    return fields


def _render_hass(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    assert tile.entity
    payload = ctx.hass.state(tile.entity)
    raw, what, attrs = _hass_reading(payload, tile.entity, tile.attribute)

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


def _grid_cell_tile(tile: TileConfig, cell: GridCell) -> TileConfig:
    """A throwaway graph config so a grid cell can reuse the tile renderers."""
    return TileConfig(
        index=tile.index,
        title=cell.title,
        source=cell.source,
        metric=cell.metric,
        chart=cell.chart,
        dimension=cell.dimension,
        entity=cell.entity,
        attribute=cell.attribute,
        appid=cell.appid,
        label=cell.label,
        unit=cell.unit,
        scale=cell.scale,
        secondary=cell.secondary,
        poll_seconds=tile.poll_seconds,
        graph_length=tile.graph_length,
        time_unit=tile.time_unit,
    )


def render_grid(tile: TileConfig, ctx: RenderContext) -> list[dict[str, str]]:
    """Render each grid cell, keeping a cell's last good value on failure."""
    cells: list[dict[str, str]] = []
    for cell in tile.cells:
        try:
            fields = render_tile(_grid_cell_tile(tile, cell), ctx)
            cell.last_fields = fields
            value = _format_subtext_number(float(fields["value"]), cell.decimals)
            unit = str(fields.get("unit") or "")
            if cell.secondary is not None:
                subtitle = cell.secondary
            else:
                subtitle = compact(str(fields.get("secondary") or ""), 40)
            cells.append(
                {
                    "title": cell.title,
                    "value": value,
                    "unit": unit,
                    "subTitle": subtitle,
                }
            )
        except SourceError as exc:
            last = cell.last_fields
            if last is not None:
                value = _format_subtext_number(float(last["value"]), cell.decimals)
                unit = str(last.get("unit") or "")
            else:
                value = "—"
                unit = cell.unit or ""
            cells.append(
                {
                    "title": cell.title,
                    "value": value,
                    "unit": unit,
                    "subTitle": compact(f"{exc.source} unavailable", 40),
                }
            )
    return cells


def _ctx_history(ctx: RenderContext) -> SeriesHistory:
    history = getattr(ctx, "history", None)
    if history is None:
        history = SeriesHistory()
        setattr(ctx, "history", history)
    return history


def _tile_history_key(tile: TileConfig) -> str:
    return f"tile:{tile.index}:{tile.title}"


def _subtext_history_key(tile: TileConfig, spec: SubtextSpec) -> str:
    parts = (
        str(tile.index),
        str(spec.slot),
        spec.source,
        spec.chart or "",
        spec.dimension or "",
        spec.entity or "",
        spec.attribute or "",
        "" if spec.appid is None else str(spec.appid),
        spec.field or "",
    )
    return "subtext:" + ":".join(parts)


def _join_unit(text: str, unit: str) -> str:
    if not unit:
        return text
    separator = " " if unit[0].isalpha() else ""
    return f"{text}{separator}{unit}"


def _format_subtext_number(value: float, decimals: int) -> str:
    if not math.isfinite(value):
        return "?"
    text = f"{value:,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _with_label(label: str | None, text: str) -> str:
    return f"{label} {text}" if label else text


def _llama_subtext_value(ctx: RenderContext, field_name: str) -> tuple[float, str]:
    model = ctx.llama.loaded_model()
    if not model:
        raise LlamaError("no model loaded")
    live = ctx.llama.live_rates(model)
    try:
        values = ctx.llama.metrics(model)
    except LlamaError:
        if live is None:
            raise
        values = {}
    pp, tg = _llama_rates(values)
    if live is not None:
        pp = live["pp"] if live["pp"] > 0 else pp
        tg = live["tg"] if live["tg"] > 0 else tg
    selected = {"pp": pp, "tg": tg, "total": pp + tg}[field_name]
    return selected, "tok/s"


def _subtext_numeric(
    tile: TileConfig,
    spec: SubtextSpec,
    ctx: RenderContext,
    primary: dict[str, Any],
) -> tuple[float, str, str]:
    """Return (value, unit, history key) for one numeric subtext source."""
    key = _subtext_history_key(tile, spec)

    if spec.source == "tile":
        return (
            float(primary["value"]) * spec.scale,
            str(primary.get("unit") or ""),
            _tile_history_key(tile),
        )

    if spec.source == "netdata":
        assert spec.chart
        values = ctx.nd.sample(spec.chart)
        if spec.dimension:
            if spec.dimension not in values:
                raise NetdataError(
                    f"{spec.chart!r} has no dimension {spec.dimension!r}"
                )
            raw = values[spec.dimension]
        else:
            if not values:
                raise NetdataError(f"{spec.chart!r} has no dimensions")
            raw = next(iter(values.values()))
        return raw * spec.scale, spec.unit or "", key

    if spec.source == "hass":
        assert spec.entity
        payload = ctx.hass.state(spec.entity)
        raw, what, attrs = _hass_reading(payload, spec.entity, spec.attribute)
        value = _hass_to_number(raw, what) * spec.scale
        unit = spec.unit
        if unit is None:
            unit = str(attrs.get("unit_of_measurement") or "")
        return value, unit, key

    if spec.source == "llama":
        value, default_unit = _llama_subtext_value(ctx, spec.field or "total")
        return value * spec.scale, spec.unit or default_unit, key

    if spec.source == "steam":
        assert spec.appid is not None and ctx.steam is not None
        value = float(ctx.steam.current_players(spec.appid)) * spec.scale
        return value, spec.unit or "players", key

    if spec.source == "plex":
        assert ctx.plex is not None
        status = ctx.plex.sessions()
        value = float(getattr(status, spec.field or "total")) * spec.scale
        return value, spec.unit or "", key

    raise ConfigError(f"Unsupported subtext source {spec.source!r}")


def _render_subtext(
    tile: TileConfig,
    spec: SubtextSpec,
    ctx: RenderContext,
    primary: dict[str, Any],
    history: SeriesHistory,
    now: float,
) -> str:
    if spec.source == "text":
        return spec.text or ""

    value, unit, key = _subtext_numeric(tile, spec, ctx, primary)
    if spec.source != "tile":
        history.record(key, now, value)

    if spec.eta is not None:
        if unit.strip() != "%":
            return _with_label(spec.label, f"eta needs % (unit {unit or 'none'})")
        return _with_label(spec.label, history.eta(key, value, spec.eta, now))

    text = _format_subtext_number(value, spec.decimals)
    if spec.unit is not None:
        unit = spec.unit
    return _with_label(spec.label, _join_unit(text, unit))


def _apply_subtexts(
    tile: TileConfig, ctx: RenderContext, fields: dict[str, Any]
) -> dict[str, Any]:
    history = _ctx_history(ctx)
    now = time.monotonic()
    specs = tile.subtexts or []

    needs_tile_history = tile.eta is not None or any(
        spec is not None and spec.source == "tile" and spec.eta is not None
        for spec in specs
    )
    if needs_tile_history:
        history.record(_tile_history_key(tile), now, float(fields["value"]))

    top = str(fields.get("subtitle") or "")
    bottom = str(fields.get("secondary") or "")
    bottom_spec = specs[1] if len(specs) > 1 else None

    # A tile-level eta is shorthand for putting the projection on the lower
    # subtext line.  An explicit custom lower line still wins.
    if tile.eta is not None and bottom_spec is None:
        unit = str(fields.get("unit") or "")
        if unit.strip() == "%":
            bottom = history.eta(
                _tile_history_key(tile), float(fields["value"]), tile.eta, now
            )
        else:
            bottom = f"eta needs % (unit {unit or 'none'})"

    for slot, spec in enumerate(specs):
        if spec is None:
            continue
        try:
            text = _render_subtext(tile, spec, ctx, fields, history, now)
        except SourceError:
            # A broken secondary source should not take the tile's own graph
            # offline; the other subtext line and the primary value still work.
            text = _with_label(spec.label, f"{spec.source} unavailable")
        if slot == 0:
            top = text
        else:
            bottom = text

    fields["subtitle"] = top
    fields["secondary"] = bottom
    return fields


NETDATA_RENDERERS = {
    "cpu": _render_cpu,
    "ram": _render_ram,
    "gpu": _render_gpu,
    "net": _render_net,
    "disk": _render_disk,
    "temp": _render_temp,
    "custom": _render_custom,
}

SOURCE_RENDERERS = {
    "llama": _render_llama,
    "hass": _render_hass,
    "steam": _render_steam,
    "plex": _render_plex,
}


def render_tile(tile: TileConfig, ctx: RenderContext) -> dict[str, Any]:
    if tile.source == "grid":
        return {"cells": render_grid(tile, ctx)}
    if tile.source == "netdata":
        renderer = NETDATA_RENDERERS[tile.metric or ""]
    else:
        renderer = SOURCE_RENDERERS[tile.source]
    fields = renderer(tile, ctx)
    if tile.eta is not None or tile.subtexts:
        fields = _apply_subtexts(tile, ctx, fields)
    return fields


def offline_fields(tile: TileConfig, error: str) -> dict[str, Any]:
    if tile.source == "grid":
        cells = tile.last_cells or [
            {
                "title": cell.title,
                "value": "—",
                "unit": cell.unit or "",
                "subTitle": "OFFLINE",
            }
            for cell in tile.cells
        ]
        return {"cells": cells}
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
        self.host = str(cfg.get("polymath_host") or "localhost")
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

        if not isinstance(keys, dict):
            raise PolymathNotReady(f"{self.keys_path} must contain a JSON object")
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
        url = f"https://{self.host}:{self.port}/v1{path}"
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
        """Extract the human-readable message and detect explicit API errors.

        Do not scan the whole message for words like "error" or "invalid":
        tile titles are echoed in responses, so a tile named "Error rate" or
        "Invalid input" would otherwise fail its own update.
        """
        if isinstance(result, dict):
            message = str(result.get("message") or "")
            explicit_error = result.get("error")
            if explicit_error:
                raise ApiError(200, str(explicit_error))
            if result.get("success") is False or result.get("ok") is False:
                raise ApiError(200, message or "Polymath reported failure")
            state = result.get("status") or result.get("state")
            if isinstance(state, str) and state.strip().lower() in {"error", "failed", "failure"}:
                raise ApiError(200, message or state)
        else:
            message = str(result or "")

        lowered = message.strip().lower()
        if "does not exist" in lowered or "not found" in lowered:
            raise TileMissing(message)
        if lowered in {"error", "failed", "failure", "invalid"} or lowered.startswith(
            ("error:", "failed:", "invalid:")
        ):
            raise ApiError(200, message)
        return message

    def _extract_tile_id(self, result: Any, message: str) -> str:
        if isinstance(result, dict):
            for key in ("id", "tileId", "tile_id", "tileID"):
                value = result.get(key)
                if isinstance(value, (str, int)):
                    text = str(value).strip()
                    if text:
                        return text
        # Older Polymath builds returned the new id in "message"; accept a
        # token-like message but not prose such as "Tile created".
        candidate = message.strip()
        if (
            candidate
            and not any(ch.isspace() for ch in candidate)
            and candidate.lower() not in {"ok", "success", "created", "added", "saved"}
        ):
            return candidate
        return ""

    @staticmethod
    def _tile_kind(tile: TileConfig) -> str:
        return "grid" if tile.source == "grid" else "graph"

    @staticmethod
    def _grid_fields(cells: list[dict[str, Any]]) -> dict[str, Any]:
        fields: dict[str, Any] = {"gridConfig": len(cells)}
        for slot, cell in enumerate(cells, 1):
            fields[f"grid{slot}"] = {
                "title": str(cell.get("title") or ""),
                "value": str(cell.get("value") or ""),
                "unit": str(cell.get("unit") or ""),
                "subTitle": str(cell.get("subTitle") or ""),
            }
        return fields

    @staticmethod
    def _graph_fields(tile: TileConfig, fields: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "currentValue": float(round(float(fields["value"]), 2)),
            "startAtZero": bool(tile.start_at_zero),
        }
        # Send empty strings too: otherwise changing a tile to a unitless or
        # empty-secondary source leaves the old values on the keyboard.
        if "unit" in fields:
            body["unit"] = str(fields["unit"] or "")
        if "subtitle" in fields:
            body["subTitle"] = str(fields["subtitle"] or "")
        if "secondary" in fields:
            body["secondarySubtitle"] = str(fields["secondary"] or "")
        if "color" in fields:
            body["color"] = str(fields["color"] or "")
        if tile.time_unit:
            body["timeUnit"] = tile.time_unit
        return body

    def _placeholder_cells(self, tile: TileConfig) -> list[dict[str, str]]:
        return [
            {
                "title": cell.title,
                "value": "—",
                "unit": cell.unit or "",
                "subTitle": "starting",
            }
            for cell in tile.cells
        ]

    def update_tile(self, tile: TileConfig, fields: dict[str, Any]) -> None:
        body: dict[str, Any] = {"id": tile.id or tile.title}
        if tile.source == "grid":
            cells = fields.get("cells")
            if not isinstance(cells, list):
                cells = self._placeholder_cells(tile)
            body.update(self._grid_fields(cells))
        else:
            body.update(self._graph_fields(tile, fields))
        try:
            result = self._request("PUT", "/config/updateGenericTile", body)
        except ApiError as exc:
            if exc.status in (404, 410):
                raise TileMissing(str(exc)) from None
            raise
        self._check_message(result)

    def create_tile(
        self, tile: TileConfig, fields: dict[str, Any] | None = None
    ) -> None:
        body: dict[str, Any] = {
            "title": tile.title,
            "type": TILE_TYPE_GRID if tile.source == "grid" else TILE_TYPE_GRAPH,
            "index": tile.index,
        }
        if tile.source == "grid":
            cells = fields.get("cells") if isinstance(fields, dict) else None
            if not isinstance(cells, list):
                cells = self._placeholder_cells(tile)
            body.update(self._grid_fields(cells))
        else:
            body["length"] = tile.graph_length
            if tile.initial_values:
                body["initialValues"] = [float(value) for value in tile.initial_values]
            if fields is not None:
                body.update(self._graph_fields(tile, fields))
            else:
                body["startAtZero"] = bool(tile.start_at_zero)
                if tile.initial_values:
                    # A seeded graph should display its newest initial point
                    # until the first live sample arrives.
                    body["currentValue"] = float(tile.initial_values[-1])
        result = self._request("POST", "/config/addGenericTile", body, timeout=60.0)
        message = self._check_message(result)
        tile.id = self._extract_tile_id(result, message) or tile.title
        tile.applied_tile_type = self._tile_kind(tile)
        if tile.source == "grid":
            tile.applied_graph_length = None
            LOG.info(
                "Created grid tile %r at index %d with %d cells (id=%r)",
                tile.title,
                tile.index,
                len(tile.cells),
                tile.id,
            )
        else:
            tile.applied_graph_length = tile.graph_length
            LOG.info(
                "Created tile %r at index %d with %d points (id=%r)",
                tile.title,
                tile.index,
                tile.graph_length,
                tile.id,
            )

    def _tile_definition_matches(self, tile: TileConfig) -> bool:
        """Whether Polymath already has this tile's type and graph length.

        Polymath's update endpoint cannot change ``length`` or convert a graph
        to a grid; only addGenericTile accepts those.  Track what was last
        applied in the state file so a change replaces the tile once, rather
        than on every start.
        """
        kind = self._tile_kind(tile)
        if tile.applied_tile_type is None:
            if kind == "grid":
                # Grids only exist in 1.5, so an unrecorded tile predates it.
                return False
            # State files from older versions have no recorded type; graph was
            # the only kind then.
            tile.applied_tile_type = "graph"
        if tile.applied_tile_type != kind:
            return False
        if kind == "grid":
            return True

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
        kind = self._tile_kind(tile)
        # Polymath cannot convert a graph into a grid (or the reverse) through
        # the update endpoint.  Recreate when the state file says the kind
        # changed; an unrecorded grid predates the 1.5 API.
        if tile.applied_tile_type != kind and (
            tile.applied_tile_type is not None or kind == "grid"
        ):
            LOG.info(
                "Tile %r changed from %s to %s; recreating it",
                tile.title,
                tile.applied_tile_type or "unknown",
                kind,
            )
            self.create_tile(tile)
            return True
        # A successful update proves the tile still exists.  Preserve the last
        # known value on reconnect instead of injecting a fake zero point.
        if tile.source == "grid":
            seed: dict[str, Any] = {
                "cells": tile.last_cells or self._placeholder_cells(tile)
            }
        else:
            seed = {
                "value": 0.0,
                "unit": tile.unit or "",
                "subtitle": "starting",
                "color": "Legend",
            }
            if tile.last_fields:
                for key in ("value", "unit", "subtitle", "secondary", "color"):
                    if key in tile.last_fields:
                        seed[key] = tile.last_fields[key]
        try:
            self.update_tile(tile, seed)
            if self._tile_definition_matches(tile):
                return False
            if tile.source == "grid":
                LOG.info("Tile %r is now a grid; recreating it", tile.title)
            else:
                LOG.info(
                    "Tile %r graph length is %s; recreating it with %d points",
                    tile.title,
                    tile.applied_graph_length if tile.applied_graph_length is not None else "unknown",
                    tile.graph_length,
                )
        except TileMissing:
            pass
        except ApiError as exc:
            if exc.status not in (404, 410):
                # A transient 500 must not clear the tile's graph history.
                raise
        self.create_tile(tile)
        return True

    def setup_tiles(self, tiles: list[TileConfig]) -> None:
        for tile in tiles:
            try:
                if self.ensure_tile(tile):
                    self.needs_save = True
            except ApiAuthError:
                raise
            except ApiError as exc:
                raise PolymathError(f"Could not prepare tile {tile.title!r}: {exc}") from None

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


def tile_type_state_key(tile: TileConfig) -> str:
    return f"type:{tile.index}:{tile.title}"


def save_state(path: Path, tiles: list[TileConfig]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {tile_state_key(tile): tile.id for tile in tiles if tile.id}
        for tile in tiles:
            if tile.applied_tile_type:
                state[tile_type_state_key(tile)] = tile.applied_tile_type
            if tile.applied_graph_length is not None:
                state[tile_length_state_key(tile)] = str(tile.applied_graph_length)
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        LOG.warning("Could not write state file %s: %s", path, exc)


class Bridge:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.user_agent = str(cfg["user_agent"])
        self.nd = NetdataClient(
            str(cfg["netdata_url"]),
            timeout=float(cfg["netdata_timeout"]),
            insecure=bool(cfg["netdata_insecure"]),
            ca=cfg.get("netdata_ca"),
            user_agent=self.user_agent,
        )
        self.llama = LlamaClient(
            str(cfg["llama_url"]),
            str(cfg.get("llama_api_key") or ""),
            timeout=float(cfg["llama_timeout"]),
            model_ttl=float(cfg["llama_model_ttl"]),
            insecure=bool(cfg["llama_insecure"]),
            ca=cfg.get("llama_ca"),
            user_agent=self.user_agent,
        )
        self.poly = PolymathClient(cfg)
        self.hass = HassClient(
            str(cfg["hass_url"]),
            str(cfg.get("hass_token") or ""),
            timeout=float(cfg["hass_timeout"]),
            insecure=bool(cfg["hass_insecure"]),
            ca=cfg.get("hass_ca"),
            user_agent=self.user_agent,
        )
        self.steam = SteamClient(
            str(cfg["steam_url"]),
            timeout=float(cfg["steam_timeout"]),
            insecure=bool(cfg["steam_insecure"]),
            ca=cfg.get("steam_ca"),
            user_agent=self.user_agent,
        )
        self.plex = PlexClient(
            str(cfg["plex_url"]),
            str(cfg.get("plex_token") or ""),
            timeout=float(cfg["plex_timeout"]),
            insecure=bool(cfg["plex_insecure"]),
            ca=cfg.get("plex_ca"),
            user_agent=self.user_agent,
        )
        self.tiles: list[TileConfig] = list(cfg["tiles"])
        self.state_path = expand_path(cfg["state_file"])
        self.state = load_state(self.state_path)
        for tile in self.tiles:
            tile.id = self.state.get(tile_state_key(tile), "")
            tile.applied_tile_type = self.state.get(tile_type_state_key(tile)) or None
            raw_length = self.state.get(tile_length_state_key(tile), "")
            if raw_length.isdigit():
                tile.applied_graph_length = int(raw_length)
        self.server = ServerInfo()
        self.ctx: RenderContext | None = None
        self.connected = False
        self.next_save_retry = 0.0
        self.next_gpu_discovery = 0.0
        self.reconnect_delay = 5.0

    def prepare(self) -> None:
        parsed = urllib.parse.urlparse(str(self.cfg["netdata_url"]))
        fallback = parsed.hostname or "server"
        info = _server_info(self.nd, fallback)
        name = str(self.cfg.get("server_name") or "").strip() or info.name or fallback
        self.server = ServerInfo(
            name=name, cores=info.cores, ram_total_mib=info.ram_total_mib
        )
        self.ctx = RenderContext(
            nd=self.nd,
            llama=self.llama,
            hass=self.hass,
            steam=self.steam,
            plex=self.plex,
            server_name=self.server.name,
            cores=self.server.cores,
            ram_total_mib=self.server.ram_total_mib,
        )
        self._discover_gpu_charts()
        for tile in self.tiles:
            if tile.source == "grid":
                LOG.info(
                    "Grid tile %r: every %gs, %d cells (%s)",
                    tile.title,
                    tile.poll_seconds,
                    len(tile.cells),
                    ", ".join(cell.title for cell in tile.cells),
                )
            else:
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
        if any(
            tile.source == "plex"
            or any(cell.source == "plex" for cell in tile.cells)
            for tile in self.tiles
        ):
            LOG.info("Plex %s", self.cfg["plex_url"])

    def _discover_gpu_charts(self) -> None:
        """Fill in an omitted GPU chart for graph tiles and grid cells."""
        for tile in self.tiles:
            specs = [tile] if tile.source != "grid" else tile.cells
            for spec in specs:
                if (
                    spec.source == "netdata"
                    and spec.metric == "gpu"
                    and not spec.chart
                ):
                    spec.chart = discover_gpu_chart(self.nd)
                    if spec.chart:
                        LOG.info(
                            "Using GPU chart %s for %r", spec.chart, spec.title
                        )
                    else:
                        LOG.warning(
                            "No nvidia_smi *_gpu_utilization chart found for %r",
                            spec.title,
                        )

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
                    if tile.source == "grid":
                        cells = " | ".join(
                            f"{cell['title']}={cell['value']}{cell['unit']} ({cell['subTitle']})"
                            for cell in fields["cells"]
                        )
                        parts.append(f"{tile.title}: {cells}")
                    else:
                        value = fields["value"]
                        unit = fields["unit"]
                        subtitle = fields.get("subtitle", "")
                        parts.append(
                            f"{tile.title}[{subtitle}]={value:.2f}{unit} ({fields['secondary']})"
                        )
                except SourceError as exc:
                    parts.append(f"{tile.title}=OFFLINE ({compact(exc, 60)})")
                self._schedule_next(tile, due)
            if parts:
                print(" | ".join(parts), flush=True)
            if once:
                return
            time.sleep(max(0.0, self._next_wake() - time.monotonic()))

    def _setup(self) -> None:
        self.poly.connect()
        try:
            self.poly.setup_tiles(self.tiles)
        finally:
            # Save once per setup even when no tile was newly created.  This
            # recovers pending saves after a restart and after a partial setup
            # failure, where some tiles were created but the loop did not finish.
            if self.tiles:
                try:
                    self.poly.save()
                except PolymathError as exc:
                    LOG.warning("Tiles could not be saved to the keyboard: %s", exc)
                    self.poly.needs_save = True
            save_state(self.state_path, self.tiles)
        self.connected = True
        # Poll every tile immediately once the connection (re)opens.
        for tile in self.tiles:
            tile.next_poll = 0.0

    def _poll_tile(self, tile: TileConfig) -> None:
        assert self.ctx is not None
        specs = [tile] if tile.source != "grid" else tile.cells
        if (
            time.monotonic() >= self.next_gpu_discovery
            and any(
                spec.source == "netdata"
                and spec.metric == "gpu"
                and not spec.chart
                for spec in specs
            )
        ):
            self._discover_gpu_charts()
            self.next_gpu_discovery = time.monotonic() + 60.0

        try:
            fields = render_tile(tile, self.ctx)
            if tile.source == "grid":
                tile.last_cells = fields["cells"]
            else:
                tile.last_fields = fields
            tile.last_ok = time.time()
            tile.last_error = None
        except SourceError as exc:
            error = str(exc)
            if tile.last_error != error:
                LOG.warning("%s metric for %r failed: %s", exc.source, tile.title, error)
            tile.last_error = error
            fields = offline_fields(tile, error)

        try:
            self.poly.update_tile(tile, fields)
            return
        except TileMissing:
            LOG.info("Tile %r is missing; recreating it", tile.title)
        except ApiAuthError as exc:
            LOG.warning("Polymath rejected the API key (HTTP %s)", exc.status)
            if exc.status == 401:
                self.poly.api_key = None
                self.poly._delete_key()
            self.connected = False
            return
        except ApiError as exc:
            # Per-tile API rejections (bad body, unsupported field, server-side
            # validation) must not take the other tiles down.
            error = f"Polymath rejected tile {exc.status}: {compact(exc.body, 80)}"
            if tile.last_error != error:
                LOG.warning("Polymath rejected tile %r update (HTTP %s): %s", tile.title, exc.status, compact(exc.body, 120))
            tile.last_error = error
            return
        except PolymathError as exc:
            LOG.warning("Lost connection to Polymath: %s", exc)
            self.connected = False
            return

        try:
            # The creation payload can carry the first value (and, for a
            # graph, initialValues), so a fresh tile appears populated without
            # a second update that would duplicate its first point.
            self.poly.create_tile(tile, fields)
            self.poly.needs_save = True
            self.poly.retry_save()
            save_state(self.state_path, self.tiles)
        except ApiAuthError as exc:
            LOG.warning("Polymath rejected the API key (HTTP %s)", exc.status)
            if exc.status == 401:
                self.poly.api_key = None
                self.poly._delete_key()
            self.connected = False
        except ApiError as exc:
            error = f"Polymath rejected tile {exc.status}: {compact(exc.body, 80)}"
            if tile.last_error != error:
                LOG.warning("Could not recreate tile %r (HTTP %s): %s", tile.title, exc.status, compact(exc.body, 120))
            tile.last_error = error
        except PolymathError as exc:
            LOG.warning("Could not recreate tile %r: %s", tile.title, exc)
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

    def _reconnect_pause(self, base: float) -> None:
        delay = min(60.0, max(base, self.reconnect_delay))
        self.reconnect_delay = min(60.0, delay * 2.0)
        time.sleep(delay)

    def run(self, once: bool = False) -> None:
        self.prepare()
        while True:
            if not self.connected:
                try:
                    self._setup()
                    self.reconnect_delay = 5.0
                except PolymathNotReady as exc:
                    LOG.warning("%s; retrying", exc)
                    if once:
                        raise
                    self._reconnect_pause(5.0)
                    continue
                except ApiAuthError as exc:
                    LOG.warning("Polymath access was denied (HTTP %s); retrying", exc.status)
                    if once:
                        raise
                    self._reconnect_pause(30.0)
                    continue
                except PolymathError as exc:
                    LOG.warning("Could not connect to Polymath: %s; retrying", exc)
                    if once:
                        raise
                    self._reconnect_pause(5.0)
                    continue

            self._poll()
            if once:
                return
            if not self.connected:
                self._reconnect_pause(5.0)
                continue
            self.reconnect_delay = 5.0
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
        prog="polymath-data-bridge",
        description=(
            "Mirror Netdata, llama.cpp, Home Assistant, Plex, and Steam "
            "metrics onto Flux Polymath graph and grid tiles."
        ),
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
    discovery = args.list_charts is not None or args.list_entities is not None
    try:
        cfg = load_config(config_path, validate_tiles=not discovery)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2

    user_agent = str(cfg["user_agent"])
    nd = NetdataClient(
        str(cfg["netdata_url"]),
        timeout=float(cfg["netdata_timeout"]),
        insecure=bool(cfg["netdata_insecure"]),
        ca=cfg.get("netdata_ca"),
        user_agent=user_agent,
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
            str(cfg["hass_url"]),
            str(cfg.get("hass_token") or ""),
            timeout=float(cfg["hass_timeout"]),
            insecure=bool(cfg["hass_insecure"]),
            ca=cfg.get("hass_ca"),
            user_agent=user_agent,
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
