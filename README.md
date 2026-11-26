# Polymath Data Bridge

Mirror [Netdata](https://www.netdata.cloud/), a
[llama.cpp](https://github.com/ggml-org/llama.cpp) server,
[Home Assistant](https://www.home-assistant.io/) entities,
[Steam](https://store.steampowered.com/) current-player counts, and
[Plex](https://www.plex.tv/) active-stream counts onto the top-bar graph and
grid tiles of a Flux keyboard through Polymath's local API.

```
Netdata / llama.cpp / hass / Steam / Plex  --HTTP(S)-->  bridge  --HTTPS localhost-->  Polymath  --USB-->  keyboard
```

Run the bridge on the same Linux machine as Polymath. It only makes outbound
HTTP(S) requests, and updates up to four graph or grid tiles. Current release:
**1.5.1**.

## Requirements

- Linux with **Polymath 1.5.0.5 or newer** running and the Flux keyboard
  attached. Grid tiles, `start_at_zero`, and `initial_values` need the 1.5
  generic-tile API.
- **Polymath API enabled**: Settings → API.
- **Netdata** reachable from the client, normally port `19999`.
- **llama.cpp** reachable with `--metrics` enabled, plus an API key if the
  server uses one.
- **Home Assistant** reachable, normally port `8123`, with a long-lived access
  token. Only needed for `hass` tiles.
- **Steam's public player-count API** reachable over HTTPS. No key is needed.
  Only needed for `steam` tiles.
- **Plex Media Server** reachable from the client, normally port `32400`, with
  the server owner's `X-Plex-Token`. Only needed for `plex` tiles.
- **Python 3.10+**. No third-party packages.
- **A systemd user session for `./install.sh`.** The installer writes a
  systemd user unit and uses `systemctl --user`. Without systemd, use the
  manual run steps below.

The first real run needs someone at the Polymath window to approve the bridge's
API prompt.

## Quick start (systemd)

```bash
./install.sh

# Set the URLs, then uncomment at least one tile.
$EDITOR ~/.config/polymath-data-bridge/config.json

# Check the data sources without talking to Polymath.
~/.local/bin/polymath-data-bridge --config ~/.config/polymath-data-bridge/config.json --dry-run --once

# First real run: approve the prompt in Polymath.
~/.local/bin/polymath-data-bridge --config ~/.config/polymath-data-bridge/config.json --once

# Start now and at login.
systemctl --user enable --now polymath-data-bridge.service
journalctl --user -u polymath-data-bridge -f
```

`install.sh` keeps an existing config. The installed example has every tile
commented out; uncomment 1–4 tiles. The first `--once` run creates and saves
the tiles.

## Run manually (without systemd)

Install the files and config:

```bash
install -d ~/.local/bin ~/.config/polymath-data-bridge
install -m 755 polymath_data_bridge.py ~/.local/bin/polymath-data-bridge

# Skip this line if you already have a config.
install -m 600 config.example.json ~/.config/polymath-data-bridge/config.json

$EDITOR ~/.config/polymath-data-bridge/config.json
```

Then test once and run in the foreground:

```bash
~/.local/bin/polymath-data-bridge --config ~/.config/polymath-data-bridge/config.json --dry-run --once
~/.local/bin/polymath-data-bridge --config ~/.config/polymath-data-bridge/config.json
```

You can also run directly from the checkout after copying and editing the
example config:

```bash
cp config.example.json config.json
$EDITOR config.json
./polymath_data_bridge.py --config config.json --dry-run --once
```

Without systemd, keep it running with `tmux`, `screen`, `nohup`, or any
supervisor you already use.

## Configuration

`config.example.json` is JSON with comments (JSONC; trailing commas are
accepted). Every tile is commented out. Uncomment one to four tiles; indexes
are `0`–`3`, and both indexes and titles must be unique. A tile is either a
graph (`source` names one service) or a grid (`source: "grid"`, with up to six
cells). Every setting is documented in the example. The ones you will normally
change:

| Field | Meaning |
|---|---|
| `netdata_url` | Base URL of the Netdata agent, e.g. `http://netdata.example.com:19999`. |
| `llama_url` | Base URL of llama.cpp, e.g. `http://llama.example.com:10000`. |
| `llama_api_key` | Bearer token for llama.cpp; use `""` if there is none. |
| `hass_url` | Home Assistant base URL, e.g. `http://homeassistant.example.com:8123`. |
| `hass_token` | Home Assistant long-lived access token. May also be supplied through `HASS_TOKEN`. |
| `steam_url` | Steam Web API base URL. Normally `https://api.steampowered.com`. |
| `plex_url` | Base URL of Plex Media Server, e.g. `http://plex.example.com:32400`. |
| `plex_token` | Plex server owner token. May also be supplied through `PLEX_TOKEN`. |
| `server_name` | Subtitle label for Netdata and Home Assistant tiles; empty uses Netdata's hostname. |
| `poll_seconds` | Default seconds between samples. Minimum `1.0`. |
| `graph_length` | Default number of graph points. |
| `tiles` | Up to four tile definitions, each a graph or a grid. |

`llama_timeout`, `llama_model_ttl`, `llama_insecure`, `llama_ca`, `hass_timeout`,
`hass_insecure`, `hass_ca`, `steam_timeout`, `steam_insecure`, `steam_ca`,
`plex_timeout`, `plex_insecure`, `plex_ca`, `netdata_timeout`,
`netdata_insecure`, `netdata_ca`, `polymath_host`, `keys_json`,
`api_key_file`, `state_file`, and `user_agent` are also documented in the
example. `user_agent` is sent to every service. The `*_ca` and `*_insecure`
options are per-source; `polymath_host` defaults to `localhost`.

### Tile sources and metrics

Every graph tile names the service it reads from with `source`. A grid tile
uses `source: "grid"` and gives each of its cells one of the same five source
names. These are also the source names used by subtext lines. Netdata exposes
several preset metrics, so a `netdata` tile or cell also names one with
`metric`:

| `source` | `metric` | Shows | Extra fields |
|---|---|---|---|
| `netdata` | `cpu` | CPU utilization | — |
| `netdata` | `ram` | Used RAM | — |
| `netdata` | `gpu` | NVIDIA GPU utilization | optional `chart` |
| `netdata` | `net` | Network throughput | optional `unit`, `scale`, `secondary` |
| `netdata` | `disk` | Used disk space | `chart`, `label` |
| `netdata` | `temp` | One Netdata sensor | `chart`, `dimension`; default thresholds use native units |
| `netdata` | `custom` | Any Netdata dimension | `chart`, `dimension`, `unit`, `scale` |
| `llama` | — | llama.cpp tokens/s, with active/queued requests in the lower subtext | — |
| `hass` | — | A Home Assistant entity or attribute | `entity`, optional `attribute`, `eta`, `subtexts` |
| `steam` | — | Current Steam players for one game | `appid`; optional `unit`, `scale`, `secondary` |
| `plex` | — | Active Plex streams, with direct/transcode/paused breakdown | optional `unit`, `scale`, `secondary`; needs `plex_token` |
| `grid` | — | Up to six compact readouts in one slot | `cells`; each cell names one of the sources above |

For example, a Netdata CPU tile and a Home Assistant tile:

```json
{ "index": 0, "title": "CPU", "source": "netdata", "metric": "cpu" }
{ "index": 1, "title": "ROOM", "source": "hass",
  "entity": "sensor.example_room_temperature", "unit": "°C" }
```

Each graph tile can override the global `poll_seconds`, `graph_length`, and
`time_unit`. The graph window is `graph_length × poll_seconds`. Changing a
tile's `graph_length` recreates that tile and clears its history.

Graph tiles also accept two Polymath 1.5 display options:

| Field | Meaning |
|---|---|
| `start_at_zero` | `true` (default) makes the y axis start at zero; `false` fits the axis to the values. |
| `initial_values` | Array of up to `graph_length` numbers used when the tile is created, so a new graph starts populated instead of blank. It is applied on creation and recreation only. |

The main graph value drops trailing decimal zeros automatically: an integer
such as a player or stream count is sent as `1`, not `1.00`; a fractional
value keeps up to two places. No per-tile setting is needed.

A `grid` tile uses one top-bar slot for up to six compact readouts. Its
`cells` array defines them in display order (`grid1` first). Each cell uses
the same source fields as a graph tile, plus a required `title`; `decimals`
(0–6, default 2), `unit`, `scale`, and `secondary` are optional. Grid cells
have no color, thresholds, ETA, or subtext lines. A cell whose source is
temporarily down keeps its last value and shows `unavailable`; the other cells
continue updating.

Grid tiles have no graph history to preserve, so the bridge reclaims their
top-bar index at every start with `addGenericTile`. That replaces a tile placed
there through Polymath's UI; graph tiles continue to be updated in place.

```json
{ "index": 3, "title": "SUMMARY", "source": "grid", "poll_seconds": 10,
  "cells": [
    { "title": "CPU", "source": "netdata", "metric": "cpu" },
    { "title": "ROOM", "source": "hass", "entity": "sensor.example_room_temperature" },
    { "title": "CS2", "source": "steam", "appid": 730, "decimals": 0 }
  ] }
```

Version 1.4 made this naming consistent: `source` is always the data source,
`metric` is only the Netdata preset, and Home Assistant settings use the
`hass_*` prefix. Older configs need their tile `metric` values and `ha_*`
settings updated. Version 1.5 adds grid tiles and the two graph display
options.

### Subtext lines and percentage ETAs

Every graph tile has two subtext lines: the upper line under the title and
the lower line under the value. Both keep useful defaults, and `subtexts` lets
you replace either line with a live readout from another source. Grid tiles
have no subtext lines; each cell uses its own `title` and `secondary`. The array
holds at most two items; index `0` is the upper line and index `1` is the
lower line. `null` keeps that line's default, a string is literal text, and
an object reads a source:

```json
"subtexts": [
  { "source": "netdata", "chart": "system.load", "dimension": "load1",
    "label": "load", "decimals": 1 },
  { "source": "hass", "entity": "sensor.battery", "label": "battery" }
]
```

| `source` | Reads | Fields |
|---|---|---|
| `netdata` | A Netdata chart dimension | `chart`, optional `dimension` |
| `hass` | A Home Assistant entity or attribute | `entity`, optional `attribute`; a `hass` tile reuses its own entity/attribute when omitted |
| `llama` | llama.cpp token rate or request count | optional `field` (`total`, `pp`, `tg`, `active`, `queued`) |
| `steam` | A game's current player count | optional `appid` (defaults to the tile's) |
| `plex` | Active Plex sessions | optional `field` (`total`, `direct`, `transcoding`, `paused`) |
| `tile` | The value plotted by the tile itself | — |
| `text` | Literal text | `text` |

The five data-source names are the same ones tiles use, plus `tile` and
`text`. Every numeric source accepts `label`, `unit`, `scale`, and `decimals`.
`label` prefixes the value, `unit` is appended, and `decimals` is the number
of places to show (default 2, trailing zeros trimmed).

For a Home Assistant graph whose resolved unit is `%`, `"eta": 100` makes the
lower subtext line show when the value will reach 100%; use `"eta": 0` for
the time to 0%. The projection is a least-squares trend over values collected
since the bridge started. Those samples are in memory only, so a restart
starts a new projection. It reports `collecting` until there are enough
samples, and `flat`, `moving away`, or `reached` when a time is not
meaningful:

```json
{ "index": 2, "title": "BATTERY", "source": "hass",
  "entity": "sensor.battery", "unit": "%", "eta": 100 }
```

`eta` can also be attached to a numeric subtext item, including a `tile`
item, to project another source or put the projection in the upper line:

```json
"subtexts": [null, { "source": "hass", "eta": 100, "label": "full" }]
```

An explicit lower `subtexts` item wins over the tile-level `eta`. A subtext
whose source is temporarily unavailable shows a short `unavailable` message
without taking the tile's own graph offline.

### Finding chart, entity, and app IDs

Chart names and entity IDs are instance-specific. List them from the client:

```bash
~/.local/bin/polymath-data-bridge --list-charts nvidia_smi
~/.local/bin/polymath-data-bridge --list-charts disk_space
~/.local/bin/polymath-data-bridge --config ~/.config/polymath-data-bridge/config.json --list-entities example
```

Use the IDs exactly as printed. For `hass`, omit `attribute` to graph the
entity state, or set it to graph one attribute instead.

A Steam `appid` is the number in the game's store URL, for example the `730`
in `https://store.steampowered.com/app/730/`. The tile title is what appears
on the keyboard; use `secondary` to override the default `appid ...` line.
Steam refreshes the count every few minutes, so a 30–60 second
`poll_seconds` is usually enough and is kinder to the API than the default.

### Plex token

A `plex` tile reads `/status/sessions`, which needs the Plex server owner's
token. On the Plex server, read it from `Preferences.xml`:

```bash
sudo grep -oP 'PlexOnlineToken="\K[^"]+' '/var/lib/plex/Plex Media Server/Preferences.xml'
```

On other installations the file is under
`/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/`.
Alternatively, open Plex Web, choose any item → `⋯` → **Get Info** →
**View XML**, and copy the `X-Plex-Token` value from the address bar.

Put the token in `plex_token`, or export `PLEX_TOKEN` before starting the
bridge. The bridge sends it in a header, never in the URL. The tile shows the
total open sessions, including paused ones; the secondary line breaks them
into direct, transcoding, and paused counts. Plex reports a `TranscodeSession`
for transcodes and direct-stream remuxes, and those sessions count as
transcoding here.

## Notes

- The bridge creates graph and grid tiles. Titles are immutable after creation,
  and adding a tile at an occupied index replaces that tile. Changing a tile
  between graph and grid, or changing a graph's `graph_length`, recreates it.
- If Netdata or a chart is unreachable, only the affected graph tile goes
  offline and keeps retrying. In a grid, only the affected cell shows
  `unavailable`; the other cells and tiles continue updating.
- A grid tile's value strings are formatted by the bridge with thousands
  separators and up to the cell's `decimals`; Polymath draws them as supplied.
- The llama tile shows aggregate prompt + generation tokens/s and the loaded
  model name. Its lower subtext line also reports active and queued requests,
  from llama.cpp's `requests_processing` and `requests_deferred` metrics. The
  server must be started with `--metrics`.
- The steam tile shows the game's current player count. It needs only the
  numeric `appid`, not a Steam Web API key.
- The plex tile shows open sessions, not library size or playback history. A
  paused stream still counts as open; the secondary line reports how many are
  paused.
- The API key is written to `api_key_file` (mode `0600`) after you approve the
  prompt. One key is used per `user_agent`, so do not run two copies with the
  same name.

## Troubleshooting

| Problem | Fix |
|---|---|
| `keys.json not found` or “API is disabled” | Start Polymath once and enable Settings → API. |
| `Netdata request failed` | Check that the client can reach `netdata_url`; Netdata may only listen on localhost. |
| `Netdata HTTP 404 for chart ...` | The chart ID is wrong. Use `--list-charts <filter>` and update the tile. |
| `llama.cpp rejected the API key` | Check `llama_api_key`; use `""` if the server has no key. |
| LLAMA tile says `no model loaded` | Load a model in the router. |
| LLAMA tile stays at `0.0` | It is idle, or llama.cpp was not started with `--metrics`. |
| `Home Assistant rejected the token` | Replace `hass_token` with a new long-lived access token. |
| `Home Assistant has no entity ...` | Check the entity ID with `--list-entities <filter>`. |
| `Steam has no player count for appid ...` | Check the numeric appid in the game's store URL. |
| `Steam request failed` | Check that the client can reach `steam_url` over HTTPS. |
| A grid cell says `unavailable` | That cell's source is down or misconfigured; the rest of the grid still updates. |
| `start_at_zero` or `initial_values` is rejected | Update Polymath to 1.5.0.5 or newer. |
| `Plex rejected the token` | Check `plex_token`, or export `PLEX_TOKEN`; see [Plex token](#plex-token). |
| `Plex request failed` | Check that the client can reach `plex_url`; Plex may only listen on localhost. |
| Tiles exist but are not on the keyboard | Check the log for a config-save error; the bridge retries every 30 s. |
| A value looks wrong | Run `--dry-run` and compare with the Netdata dashboard; adjust `scale` or `unit`. |

## Uninstall

```bash
systemctl --user disable --now polymath-data-bridge.service 2>/dev/null || true
rm -f ~/.local/bin/polymath-data-bridge \
      ~/.config/systemd/user/polymath-data-bridge.service
rm -rf ~/.config/polymath-data-bridge ~/.local/state/polymath-data-bridge
systemctl --user daemon-reload 2>/dev/null || true
```

For a manual install, remove the same binary, config, and state paths. Tiles
remain in Polymath's `mosaic.json` until you replace or remove them there.

## Tests

```bash
python3 -m unittest discover -s tests
```
