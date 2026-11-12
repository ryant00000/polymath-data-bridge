# Flux Netdata + llama.cpp Bridge

Mirror a remote [Netdata](https://www.netdata.cloud/) agent, a
[llama.cpp](https://github.com/ggml-org/llama.cpp) server, and
[Home Assistant](https://www.home-assistant.io/) entities onto the top-bar graph
tiles of a Flux keyboard, through Polymath's local API.

```
Netdata / llama.cpp / HA  --HTTP-->  this bridge  --HTTPS localhost-->  Polymath  --USB-->  keyboard
```

This is the "local sidecar" option: the bridge runs on the Linux machine that has
the keyboard, pulls metrics from the server, and updates up to four Polymath
graph tiles. Polymath's API stays bound to localhost; no port forwarding or
certificate copying is needed.

## Requirements

- Linux client with **Polymath 1.4.5.x** running, and the Flux keyboard attached.
- **Polymath API enabled**: Settings → API → enable it.
- **Netdata** reachable from the client over HTTP(S), normally port `19999`.
- **llama.cpp server** reachable from the client, with `--metrics` enabled and
  an API key if the server requires one. Router mode is supported.
- **Home Assistant** reachable from the client (normally port `8123`) with a
  **long-lived access token**, only if you use `hass` tiles.
- Python **3.10+**. No third-party packages; standard library only.

The bridge does not need root, a GUI, or a keyboard at install time. The first
real run needs someone at the Polymath window to approve the API prompt.

## Quick start

```bash
# On the Linux client, from this directory:
./install.sh

# 1. Edit the config: set netdata_url/llama_url and uncomment at least one tile.
$EDITOR ~/.config/flux-netdata-bridge/config.json

# 2. Check both data sources without touching Polymath.
~/.local/bin/flux-netdata-bridge \
  --config ~/.config/flux-netdata-bridge/config.json \
  --dry-run --once

# 3. First real run. Approve "Flux Netdata Bridge" in Polymath.
~/.local/bin/flux-netdata-bridge \
  --config ~/.config/flux-netdata-bridge/config.json \
  --once

# 4. Run it at login.
systemctl --user enable --now flux-netdata-bridge.service
journalctl --user -u flux-netdata-bridge -f
```

Step 3 creates the tiles and calls `POST /config/save`. If the tiles do not
appear on the keyboard, check the log and see “First run” below.

## Configuration

`config.example.json` is installed to `~/.config/flux-netdata-bridge/config.json`.
It is JSON with `//` comments (JSONC; trailing commas are accepted), and every
tile example is commented out. Uncomment at least one tile and keep at most
four active tiles. The important fields:

| Field | Meaning |
|---|---|
| `netdata_url` | Base URL of the Netdata agent, e.g. `http://netdata.example.com:19999`. Use the address the **client** can reach. |
| `llama_url` | Base URL of llama.cpp, e.g. `http://llama.example.com:10000`. Use `http://127.0.0.1:10000` if it runs on the keyboard host. |
| `llama_api_key` | API key sent as `Authorization: Bearer ...`. Set empty if the server has no key. |
| `llama_timeout` | Per-request timeout for llama.cpp. |
| `llama_model_ttl` | Seconds to cache the loaded-model lookup. `0` checks `/v1/models` every poll (safest when models switch). |
| `ha_url` | Base URL of Home Assistant, e.g. `http://homeassistant.example.com:8123`. Only needed for `hass` tiles. |
| `ha_token` | Home Assistant **long-lived access token** (Profile → Security → Long-lived access token). Sent as `Authorization: Bearer ...`. May be left empty and supplied via the `HA_TOKEN` environment variable instead. Keep the config file mode `0600`. |
| `ha_timeout` | Per-request timeout for Home Assistant. |
| `server_name` | Label shown in the non-llama tile subtitles. If empty, the hostname reported by Netdata is used. |
| `poll_seconds` | Default polling interval for tiles that do not set their own. `2.0` is a good default. Minimum `1.0`. |
| `graph_length` | Default number of points for tiles that do not set their own. `120` × 2 s = four minutes. Max 100000. |
| `time_unit` | Default text under the graph. If `null`, the bridge derives `s` or `× 2 s` from the tile's own `poll_seconds`. |
| `keys_json` | Polymath's `keys.json`. Linux default: `~/flux/config/keys.json`. |
| `api_key_file` | Where the bridge stores its Polymath API key. Mode `0600`. |
| `state_file` | Tile-ID and applied graph-length cache. Safe to delete; the bridge recreates missing tiles (and reapplies an explicit `graph_length`). |
| `netdata_timeout` | Per-request timeout for Netdata. |
| `netdata_insecure` | Set `true` only for an HTTPS Netdata with a self-signed cert. |
| `netdata_ca` | Optional CA bundle for HTTPS Netdata. |
| `tiles` | Tile definitions. The example ships with all of them commented out; uncomment 1–4. Indexes are `0–3`; indexes and titles must be unique. Each tile also accepts its own `poll_seconds`, `graph_length`, and `time_unit`; see below. |

### Tile definitions

Built-in metric types:

| `metric` | Value shown | Default unit | Extra fields |
|---|---|---|---|
| `cpu` | Total CPU utilization (`system.cpu`) | `%` | — |
| `ram` | Used RAM (`system.ram`) | `%` | — |
| `gpu` | NVIDIA GPU utilization (`nvidia_smi.*_gpu_utilization`) | `%` | optional `chart`, `dimension` |
| `llama` | Aggregate llama.cpp tokens/s (`pp + tg`) | `tok/s` | — |
| `net` | Combined RX+TX (`system.net`) | `Mbps` | — |
| `disk` | Used space | `%` | **`chart`** (required), `label` |
| `temp` | One sensor value | `°C` | **`chart`** (required), `dimension` |
| `custom` | Any one Netdata dimension | — | **`chart`**, `dimension`, `unit`, `scale`, `warn`, `crit` |
| `hass` | Any Home Assistant entity state or attribute | auto (`unit_of_measurement`) | **`entity`** (required), `attribute`, `unit`, `scale`, `warn`, `crit` |

### Per-tile time window and polling

`poll_seconds`, `graph_length`, and `time_unit` can also be set on an
individual tile. A tile's value wins. Omitted (or `null`) `poll_seconds` and
`graph_length` inherit the global defaults. `time_unit` is derived from the
tile's own interval unless set; an explicit global `time_unit` is used only by
tiles that keep the global interval.

The graph's visible time span is `graph_length × poll_seconds`. For example,
to keep 30 minutes of CPU at 5-second resolution while sampling GPU every
second:

```json
{ "index": 0, "title": "CPU", "metric": "cpu",
  "poll_seconds": 5, "graph_length": 360 },
{ "index": 2, "title": "GPU", "metric": "gpu",
  "poll_seconds": 1, "graph_length": 240 }
```

- Each tile is polled on its own clock; a slow tile does not hold up a fast
  one.
- Unless `time_unit` is set explicitly, the label follows that tile's own
  interval: a 5-second tile shows `× 5 s`, a 1-second tile shows `s`.
- Polymath only accepts the graph length when a tile is created. If a tile's
  `graph_length` changes, the bridge recreates that tile once on the next
  start, which clears its graph history. A `poll_seconds` change takes effect
  on the next poll after restarting the bridge and needs no save.
- A tile that should use both globals can omit all three fields.

`gpu` without a `chart` auto-selects the first `nvidia_smi.*_gpu_utilization`
chart. If the machine has several GPUs, set `chart` explicitly to the one you
want.

The shipped example includes one commented tile for each data source: a
Netdata chart, the llama.cpp tile, and a Home Assistant entity. Uncomment the
ones you need. To add another Netdata view, for example network instead of
GPU:

```json
{ "index": 2, "title": "NET", "metric": "net" }
```

A disk tile uses a Netdata chart ID, for example:

```json
{ "index": 3, "title": "DISK", "metric": "disk",
  "chart": "disk_space./", "label": "/" }
```

Chart IDs are instance-specific. Use whatever `--list-charts disk_space`
shows for the mount you want.

### The LLAMA tile

The `llama` metric reads the loaded model and live throughput from the
llama.cpp server:

- Value: **aggregate tokens/s** = prompt-processing + generation, live while
  a request is running.
- Subtitle: the **loaded model ID** (replacing `server_name`).
- Secondary line: **`pp <x> · tg <y>`**, where `pp` is prompt-processing
  tokens/s and `tg` is text-generation tokens/s.
- When no model is loaded: value `0`, subtitle `no model loaded`, secondary
  `pp 0.0 · tg 0.0`.

**Why live rates come from `/slots`:** llama.cpp's `*_tokens_seconds` gauges
are buckets that are only filled when a request's slot is *released*, so during
a generation the gauges read `0` and the cumulative counters are frozen; you
get a single spike on the scrape right after completion and nothing while the
model is actually working. The bridge therefore samples `/slots` every poll —
its per-slot `next_token[0].n_decoded` and `n_prompt_tokens_processed` advance
in real time — and differentiates them between polls (keyed by task id, so a
new request never registers as a huge burst). The `/metrics` gauges remain as
a fallback when `/slots` is unavailable. Rates are server-wide (aggregate
across all slots), not per request.

**Loaded-model handling:** in router mode the bridge reads `/v1/models`, picks
the entry whose status is `loaded`, and then requests
`/metrics?model=<id>` and `/slots?model=<id>`. It only queries the loaded
model. With `llama_model_ttl: 0` it re-checks `/v1/models` every poll, so it
does not hold a stale model name and does not ask for an unloaded one. Avoid
manually querying `/metrics?model=` for an unloaded model; depending on router
settings it can hang or trigger an autoload that evicts the current model.

The API key is sent as `Authorization: Bearer <llama_api_key>`. The server must
be started with `--metrics`; otherwise `/metrics` is not available.

### The HASS tile (any entity state or attribute)

The `hass` metric reads the Home Assistant REST API (`GET /api/states/...`,
read-only; the bridge never calls services) with a long-lived access token, and
graphs the result:

- **Entity state** (the default): the entity's state is converted to a number
  and graphed. The unit defaults to the entity's own `unit_of_measurement`, and
  the secondary line shows the entity's `friendly_name`.
- **Any attribute**: add `"attribute": "<name>"` to graph that attribute of the
  entity instead of its state — e.g. a climate's target `temperature`, a
  water heater's `operation_time`, or a battery's `capacity`.

`on`/`off` and `true`/`false` states map to `1`/`0`. States of `unknown` or
`unavailable`, a missing entity, a missing attribute, or a non-numeric value put
just that tile offline (other tiles keep updating).

For example, a vehicle's state-of-charge sensor can be graphed directly:

```json
{ "index": 0, "title": "EV", "metric": "hass", "entity": "sensor.example_ev_soc" }
```

To graph an attribute instead of the state, name it explicitly:

```json
{ "index": 1, "title": "HVAC", "metric": "hass",
  "entity": "climate.example_room", "attribute": "current_temperature" }
```

`scale`, `unit`, `secondary`, `warn`, and `crit` behave as with `custom`, except
`warn`/`crit` must both be present and mark *high* values as bad (same direction
as every other tile).

### Finding entities and chart IDs

Netdata chart names are instance-specific. List them from the client:

```bash
~/.local/bin/flux-netdata-bridge --list-charts disk_space
~/.local/bin/flux-netdata-bridge --list-charts temperature
~/.local/bin/flux-netdata-bridge --list-charts nvidia_smi
~/.local/bin/flux-netdata-bridge --list-charts amdgpu
```

The output is `chart-id <TAB> units <TAB> title`. Use the chart ID verbatim in
`chart`.

Home Assistant entity IDs are likewise instance-specific. List them from the
client (needs `ha_url` and `ha_token` in the config, or `HA_TOKEN`):

```bash
~/.local/bin/flux-netdata-bridge --config ~/.config/flux-netdata-bridge/config.json --list-entities example
```

The output is `entity-id <TAB> state <TAB> unit <TAB> friendly name`. Use the
entity ID verbatim in `entity`; to see an entity's available attributes, look at
`http://<ha>:8123/developer-tools/state`.

## What appears on the keyboard

This bridge only creates **graph** tiles (Polymath tile type `1`). Each tile has:

- an immutable title (also the tile ID),
- one large current value + unit,
- one subtitle line and one secondary line,
- a graph with that tile's `graph_length` points, updated on that tile's
  `poll_seconds`.

The graph is one value per tile. To show more metrics, use up to four tiles.
Adding a tile at an occupied index **replaces** whatever was there. Back up
`~/flux/config/mosaic.json` before the first run if the top bar is already
configured.

Because the API graph labels are one line each, this cannot reproduce Polymath's
multi-line built-in **Hardware Status Text** tile. It is a compact graph view,
not a clone of the built-in panel.

If Netdata is unreachable, the bridge keeps the last good value, changes the
subtitle to `OFFLINE`, adds the last update time, and colors the tile red. It
keeps retrying. If a single chart disappears, only that tile goes offline; the
others keep updating. The llama tile follows the same rule when `/v1/models` or
`/metrics` fails.

## First run / API key

The first `--once` run calls `POST /authentication/register`. Polymath shows a
permission prompt; approve it. The key is written to `api_key_file` (mode `0600`)
and reused on later runs.

- If the prompt is denied, the bridge logs HTTP 403 and retries every 30 s.
- If the key is revoked, the next run sees HTTP 401 and asks for a new one.
- One key per application name. Do not run two copies with the same
  `user_agent`; they invalidate each other's key.
- The API key is per Polymath installation. If you move the config to another
  machine, delete `api_key_file` and approve again.

The bridge calls `GET /docs` and `GET /authentication/check`; it does not expose
any network port itself.

## Troubleshooting

**`keys.json not found`** — Start Polymath once, enable Settings → API, then run
again. On Linux the file is `~/flux/config/keys.json`.

**`The Polymath API is disabled`** — Enable it in Settings → API. The bridge
does not enable it for you.

**`Could not find Polymath's TLS certificate`** — Check `publicKeyLocation` in
`keys.json`. The fallback is
`~/.local/share/com.fluxkeyboard.polymath/tls/cert.pem`.

**`Netdata HTTP 404 for chart ...`** — The chart ID is wrong or the collector is
not enabled. Use `--list-charts <filter>`; the bridge keeps the other tiles
running.

**`Netdata request failed`** — The client cannot reach `netdata_url`. Test with
`curl http://server:19999/api/v1/info`. Check Netdata's `bind to` setting and the
firewall; Netdata often only listens on localhost by default.

**`llama.cpp rejected the API key (HTTP 401)`** — Check `llama_api_key`. If the
server has no key, set it to `""`.

**`Home Assistant rejected the token (HTTP 401/403)`** — The `ha_token` is
expired, revoked, or not a long-lived access token. Create a new one in
Profile → Security → Long-lived access token.

**`Home Assistant has no entity '...'`** — The `entity` ID is wrong. Use
`--list-entities <filter>` to find it.

**`... has no attribute '...'` / `... is not numeric`** — Graphing needs a
number (or `on`/`off`). Check the entity's attributes on the Developer Tools →
State page; `unknown`/`unavailable` states and text attributes cannot be
graphed.

**`llama.cpp HTTP 400 ...model name is missing`** — The server is in router mode
and the bridge did not send a model. This normally means `/v1/models` did not
return a loaded model; check that a model is loaded, or set `llama_model_ttl: 0`.

**LLAMA tile says `no model loaded`** — `/v1/models` has no entry with status
`loaded`. Load a model in the router; the bridge will pick it up on the next
poll.

**LLAMA tile is always `0.0`** — It is idle, or the server was not started with
`--metrics`. Run a generation and check `--dry-run`: the tile should show a
non-zero `tg` while it runs. If `tg` stays `0` even during
generation, check that the router proxies `/slots` (the log says so if it is
missing).

**Tiles exist but are not on the keyboard** — New tiles need
`POST /config/save`. The bridge does that after creation. Check the log for
“Saved the keyboard configuration”; if saving failed, it retries every 30 s.
Also confirm the tile indexes are not occupied by tiles you wanted to keep.

**GPU tile is `OFFLINE`** — The bridge auto-selects the first
`nvidia_smi.*_gpu_utilization` chart. If the GPU is not collected by Netdata, or
you want a specific card, run `--list-charts nvidia_smi` and put the chosen
chart ID in the tile's `chart`.

**The value looks wrong** — Netdata units differ by chart. Run `--dry-run` and
compare with the Netdata dashboard. For `custom`, use `scale` to convert units.
For llama.cpp, remember the tile value is `pp + tg`, not just generation.

## Limitations

- Four top-bar tiles maximum; one graph value each.
- No text-only generic tile; the title is immutable after creation.
- The bridge must be running on the same machine as Polymath; Polymath must be
  running for updates to reach the keyboard.
- The built-in Hardware Status tile still shows the local machine. This bridge
  adds separate tiles for the remote server; it does not replace the built-in
  provider.
- Graph history lives in Polymath. The bridge only supplies the current value;
  restarting Polymath or the bridge may start a fresh graph, and changing a
  tile's `graph_length` recreates that tile and clears its history.
- The llama tile is a throughput graph, not a per-request or per-slot table.
  Aggregate means server-wide `pp + tg` across the scrape window.

## Uninstall

```bash
systemctl --user disable --now flux-netdata-bridge.service
rm -f ~/.local/bin/flux-netdata-bridge \
      ~/.config/systemd/user/flux-netdata-bridge.service
rm -rf ~/.config/flux-netdata-bridge ~/.local/state/flux-netdata-bridge
systemctl --user daemon-reload
```

The tiles remain in Polymath's `mosaic.json` until you replace or remove them in
Polymath.
