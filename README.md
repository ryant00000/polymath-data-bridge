# Polymath Data Bridge

Mirror [Netdata](https://www.netdata.cloud/), a
[llama.cpp](https://github.com/ggml-org/llama.cpp) server,
[Home Assistant](https://www.home-assistant.io/) entities,
[Steam](https://store.steampowered.com/) current-player counts, and
[Plex](https://www.plex.tv/) active-stream counts onto the top-bar graph tiles
of a Flux keyboard through Polymath's local API.

```
Netdata / llama.cpp / HA / Steam / Plex  --HTTP(S)-->  bridge  --HTTPS localhost-->  Polymath  --USB-->  keyboard
```

Run the bridge on the same Linux machine as Polymath. It only makes outbound
HTTP(S) requests, and updates up to four graph tiles. Current release: **1.3**.

## Requirements

- Linux with **Polymath 1.4.5.x** running and the Flux keyboard attached.
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
are `0`–`3`, and both indexes and titles must be unique. Every setting is
documented in the example. The ones you will normally change:

| Field | Meaning |
|---|---|
| `netdata_url` | Base URL of the Netdata agent, e.g. `http://netdata.example.com:19999`. |
| `llama_url` | Base URL of llama.cpp, e.g. `http://llama.example.com:10000`. |
| `llama_api_key` | Bearer token for llama.cpp; use `""` if there is none. |
| `ha_url` | Home Assistant base URL, e.g. `http://homeassistant.example.com:8123`. |
| `ha_token` | Home Assistant long-lived access token. May also be supplied through `HA_TOKEN`. |
| `steam_url` | Steam Web API base URL. Normally `https://api.steampowered.com`. |
| `plex_url` | Base URL of Plex Media Server, e.g. `http://plex.example.com:32400`. |
| `plex_token` | Plex server owner token. May also be supplied through `PLEX_TOKEN`. |
| `server_name` | Subtitle label for Netdata and Home Assistant tiles; empty uses Netdata's hostname. |
| `poll_seconds` | Default seconds between samples. Minimum `1.0`. |
| `graph_length` | Default number of graph points. |
| `tiles` | Up to four tile definitions. |

`llama_timeout`, `llama_model_ttl`, `llama_insecure`, `llama_ca`, `ha_timeout`,
`ha_insecure`, `ha_ca`, `steam_timeout`, `steam_insecure`, `steam_ca`,
`plex_timeout`, `plex_insecure`, `plex_ca`, `netdata_timeout`,
`netdata_insecure`, `netdata_ca`, `polymath_host`, `keys_json`,
`api_key_file`, `state_file`, and `user_agent` are also documented in the
example. `user_agent` is sent to every service. The `*_ca` and `*_insecure`
options are per-source; `polymath_host` defaults to `localhost`.

### Tile types

| `metric` | Shows | Extra fields |
|---|---|---|
| `cpu` | CPU utilization | — |
| `ram` | Used RAM | — |
| `gpu` | NVIDIA GPU utilization | optional `chart` |
| `llama` | llama.cpp tokens/s | — |
| `net` | Network throughput | optional `unit`, `scale`, `secondary` |
| `disk` | Used disk space | `chart`, `label` |
| `temp` | One Netdata sensor | `chart`, `dimension`; default thresholds use native units |
| `custom` | Any Netdata dimension | `chart`, `dimension`, `unit`, `scale` |
| `hass` | A Home Assistant entity or attribute | `entity`, optional `attribute` |
| `steam` | Current Steam players for one game | `appid`; optional `unit`, `scale`, `secondary` |
| `plex` | Active Plex streams, with direct/transcode/paused breakdown | optional `unit`, `scale`, `secondary`; needs `plex_token` |

Each tile can override the global `poll_seconds`, `graph_length`, and
`time_unit`. The graph window is `graph_length × poll_seconds`. Changing a
tile's `graph_length` recreates that tile and clears its history.

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

- The bridge creates graph tiles only. Titles are immutable after creation,
  and adding a tile at an occupied index replaces that tile.
- If Netdata or a chart is unreachable, only the affected tile goes offline
  and keeps retrying; the other tiles continue updating.
- The llama tile shows aggregate prompt + generation tokens/s and the loaded
  model name. The server must be started with `--metrics`.
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
| `Home Assistant rejected the token` | Replace `ha_token` with a new long-lived access token. |
| `Home Assistant has no entity ...` | Check the entity ID with `--list-entities <filter>`. |
| `Steam has no player count for appid ...` | Check the numeric appid in the game's store URL. |
| `Steam request failed` | Check that the client can reach `steam_url` over HTTPS. |
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
