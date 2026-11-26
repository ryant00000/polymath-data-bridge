import io
import json
import os
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import polymath_data_bridge as f


BASE_CONFIG = {
    "netdata_url": "http://netdata.example.com:19999",
    "llama_url": "http://llama.example.com:10000",
    "hass_url": "http://homeassistant.example.com:8123",
    "tiles": [{"index": 0, "title": "CPU", "source": "netdata", "metric": "cpu"}],
}


def load_config_with(patch, **kwargs):
    cfg = dict(BASE_CONFIG)
    cfg.update(patch)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(cfg, fh)
        name = fh.name
    try:
        return f.load_config(Path(name), **kwargs)
    finally:
        Path(name).unlink(missing_ok=True)


def tile(**kwargs):
    values = {"index": 0, "title": "T", "source": "netdata"}
    values.update(kwargs)
    if values["source"] != "netdata":
        values.pop("metric", None)
    return f.TileConfig(**values)


class FakeND:
    def __init__(self, data):
        self.data = data

    def sample(self, chart):
        return self.data[chart]


class FakeCtx:
    def __init__(self, nd, ram_total_mib=1000.0, steam=None, plex=None):
        self.nd = nd
        self.llama = None
        self.hass = None
        self.steam = steam
        self.plex = plex
        self.server_name = "host"
        self.cores = 8
        self.ram_total_mib = ram_total_mib


class ConfigTests(unittest.TestCase):
    def test_rejects_bad_numbers_and_typos(self):
        cases = [
            {"warn": "high"},
            {"poll_seconds": float("nan")},
            {"graph_length": float("inf")},
            {"tiles": [{"index": 0.9, "title": "CPU", "source": "netdata", "metric": "cpu"}]},
            {"tiles": [{"index": 0, "title": "CPU", "source": "netdata", "metric": "cpu", "warn": 80}]},
            {"netdata_insecure": "false"},
            {"steam_timeout": 0},
            {"steam_url": "ftp://steam.example.com"},
            {"plex_timeout": 0},
            {"plex_url": "ftp://plex.example.com"},
            {"tile": []},
            {"tiles": [{"index": 0, "title": "CPU", "source": "netdata", "metric": "cpu", "colour": "red"}]},
            {"tiles": [{"index": 0, "title": "CPU", "metric": "cpu"}]},
            {"tiles": [{"index": 0, "title": "CPU", "source": "netdata"}]},
            {"tiles": [{"index": 0, "title": "CPU", "source": "netdata", "metric": "nope"}]},
            {"tiles": [{"index": 0, "title": "S", "source": "steam"}]},
            {"tiles": [{"index": 0, "title": "S", "source": "steam", "appid": 0}]},
            {"tiles": [{"index": 0, "title": "S", "source": "steam", "appid": 1.5}]},
            {"tiles": [{"index": 0, "title": "S", "source": "steam", "appid": "730x"}]},
            {"tiles": [{"index": 0, "title": "CPU", "source": "netdata", "metric": "cpu", "appid": 730}]},
            {"tiles": [{"index": 0, "title": "CPU", "source": "llama", "chart": "system.cpu"}]},
            {"tiles": [{"index": 0, "title": "CPU", "source": "llama", "dimension": "user"}]},
            {"tiles": [{"index": 0, "title": "CPU", "source": "llama", "label": "x"}]},
            {"tiles": [{"index": 0, "title": "CPU", "source": "hass", "metric": "cpu", "entity": "sensor.x"}]},
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                with self.assertRaises(f.ConfigError):
                    load_config_with(patch)

    def test_accepts_discovery_mode_without_valid_tiles(self):
        cfg = load_config_with(
            {"tiles": [{"index": 99, "title": "", "source": "nope"}]},
            validate_tiles=False,
        )
        self.assertEqual(cfg["tiles"], [])

    def test_example_config_loads(self):
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        cfg = f.load_config(example)
        self.assertEqual(cfg["tiles"], [])
        self.assertEqual(cfg["hass_url"], "http://homeassistant.example.com:8123")

    def test_new_options_are_loaded(self):
        cfg = load_config_with(
            {
                "llama_insecure": True,
                "llama_ca": "/tmp/llama.pem",
                "hass_insecure": True,
                "hass_ca": "/tmp/hass.pem",
                "steam_insecure": True,
                "steam_ca": "/tmp/steam.pem",
                "polymath_host": "127.0.0.1",
            }
        )
        self.assertTrue(cfg["llama_insecure"])
        self.assertTrue(cfg["hass_insecure"])
        self.assertTrue(cfg["steam_insecure"])
        self.assertEqual(cfg["polymath_host"], "127.0.0.1")

    def test_source_and_netdata_metric_are_loaded(self):
        cfg = load_config_with(
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "CPU",
                        "source": "netdata",
                        "metric": "custom",
                        "chart": "system.cpu",
                    }
                ]
            }
        )
        t = cfg["tiles"][0]
        self.assertEqual(t.source, "netdata")
        self.assertEqual(t.metric, "custom")

    def test_hass_settings_and_environment_token_are_loaded(self):
        with mock.patch.dict(os.environ, {"HASS_TOKEN": "env-token"}):
            cfg = load_config_with({"hass_insecure": True, "hass_ca": "/tmp/hass.pem"})
        self.assertEqual(cfg["hass_url"], "http://homeassistant.example.com:8123")
        self.assertEqual(cfg["hass_token"], "env-token")
        self.assertTrue(cfg["hass_insecure"])
        self.assertEqual(cfg["hass_ca"], "/tmp/hass.pem")

    def test_steam_tile_loads_appid(self):
        cfg = load_config_with(
            {"tiles": [{"index": 0, "title": "CS2", "source": "steam", "appid": "730"}]}
        )
        self.assertEqual(cfg["tiles"][0].appid, 730)

    def test_plex_tile_requires_a_token(self):
        with mock.patch.dict(os.environ, {"PLEX_TOKEN": ""}):
            with self.assertRaisesRegex(f.ConfigError, "plex_token"):
                load_config_with(
                    {"tiles": [{"index": 0, "title": "PLEX", "source": "plex"}]}
                )

    def test_plex_subtext_requires_a_token(self):
        with mock.patch.dict(os.environ, {"PLEX_TOKEN": ""}):
            with self.assertRaisesRegex(f.ConfigError, "plex_token"):
                load_config_with(
                    {
                        "tiles": [
                            {
                                "index": 0,
                                "title": "CPU",
                                "source": "netdata",
                                "metric": "cpu",
                                "subtexts": [{"source": "plex"}],
                            }
                        ]
                    }
                )

    def test_eta_and_subtexts_are_loaded(self):
        cfg = load_config_with(
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "BATT",
                        "source": "hass",
                        "entity": "sensor.battery",
                        "unit": "%",
                        "eta": 100,
                        "subtexts": [
                            {
                                "source": "netdata",
                                "chart": "system.load",
                                "dimension": "load1",
                                "label": "load",
                            },
                            {"source": "hass", "eta": 0},
                        ],
                    }
                ]
            }
        )
        t = cfg["tiles"][0]
        self.assertEqual(t.source, "hass")
        self.assertIsNone(t.metric)
        self.assertEqual(t.eta, 100)
        self.assertEqual(t.subtexts[0].source, "netdata")
        self.assertEqual(t.subtexts[0].chart, "system.load")
        self.assertEqual(t.subtexts[0].label, "load")
        self.assertEqual(t.subtexts[1].source, "hass")
        self.assertEqual(t.subtexts[1].entity, "sensor.battery")
        self.assertEqual(t.subtexts[1].eta, 0)

    def test_bad_eta_and_subtexts_are_rejected(self):
        cases = [
            {"tiles": [{"index": 0, "title": "B", "source": "hass", "entity": "sensor.x", "eta": 50}]},
            {"tiles": [{"index": 0, "title": "C", "source": "netdata", "metric": "cpu", "eta": 100}]},
            {"tiles": [{"index": 0, "title": "B", "source": "hass", "entity": "sensor.x", "unit": "W", "eta": 100}]},
            {"tiles": [{"index": 0, "title": "C", "source": "netdata", "metric": "cpu", "subtexts": ["a", "b", "c"]}]},
            {"tiles": [{"index": 0, "title": "C", "source": "netdata", "metric": "cpu", "subtexts": [{"source": "nope"}]}]},
            {"tiles": [{"index": 0, "title": "C", "source": "netdata", "metric": "cpu", "subtexts": [{"source": "netdata"}]}]},
            {"tiles": [{"index": 0, "title": "C", "source": "netdata", "metric": "cpu", "subtexts": [{"source": "hass"}]}]},
            {"tiles": [{"index": 0, "title": "C", "source": "netdata", "metric": "cpu", "subtexts": [{"source": "steam"}]}]},
            {"tiles": [{"index": 0, "title": "C", "source": "netdata", "metric": "cpu", "subtexts": [{"source": "text"}]}]},
            {"tiles": [{"index": 0, "title": "C", "source": "netdata", "metric": "cpu", "subtexts": [{"source": "text", "text": "x", "unit": "%"}]}]},
            {"tiles": [{"index": 0, "title": "C", "source": "netdata", "metric": "cpu", "subtexts": [{"source": "netdata", "chart": "x", "bogus": 1}]}]},
            {"tiles": [{"index": 0, "title": "C", "source": "netdata", "metric": "cpu", "subtexts": [{"source": "netdata", "chart": "x", "decimals": 9}]}]},
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                with self.assertRaises(f.ConfigError):
                    load_config_with(patch)

    def test_grid_tile_loads_cells(self):
        cfg = load_config_with(
            {
                "tiles": [
                    {
                        "index": 3,
                        "title": "SUMMARY",
                        "source": "grid",
                        "poll_seconds": 5,
                        "cells": [
                            {
                                "title": "CPU",
                                "source": "netdata",
                                "metric": "cpu",
                            },
                            {
                                "title": "ROOM",
                                "source": "hass",
                                "entity": "sensor.room",
                                "unit": "°C",
                                "decimals": 1,
                            },
                            {
                                "title": "CS2",
                                "source": "steam",
                                "appid": 730,
                                "secondary": "players",
                            },
                        ],
                    }
                ]
            }
        )
        t = cfg["tiles"][0]
        self.assertEqual(t.source, "grid")
        self.assertEqual([cell.title for cell in t.cells], ["CPU", "ROOM", "CS2"])
        self.assertEqual(t.cells[0].metric, "cpu")
        self.assertEqual(t.cells[1].entity, "sensor.room")
        self.assertEqual(t.cells[1].unit, "°C")
        self.assertEqual(t.cells[1].decimals, 1)
        self.assertEqual(t.cells[2].appid, 730)
        self.assertEqual(t.cells[2].secondary, "players")
        self.assertEqual(t.poll_seconds, 5.0)
        self.assertEqual(t.time_unit, "× 5 s")
        self.assertTrue(t.start_at_zero)
        self.assertIsNone(t.initial_values)

    def test_bad_grid_tiles_are_rejected(self):
        cell = {"title": "CPU", "source": "netdata", "metric": "cpu"}
        cases = [
            {"tiles": [{"index": 0, "title": "G", "source": "grid", "cells": []}]},
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [dict(cell, title=f"T{i}") for i in range(7)],
                    }
                ]
            },
            {"tiles": [{"index": 0, "title": "G", "source": "grid"}]},
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [{"title": "X", "source": "grid"}],
                    }
                ]
            },
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [{"source": "netdata", "metric": "cpu"}],
                    }
                ]
            },
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [cell, cell],
                    }
                ]
            },
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [{"title": "X", "source": "netdata"}],
                    }
                ]
            },
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [{"title": "X", "source": "hass"}],
                    }
                ]
            },
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [{"title": "X", "source": "steam"}],
                    }
                ]
            },
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [dict(cell, decimals=7)],
                    }
                ]
            },
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [dict(cell, poll_seconds=2)],
                    }
                ]
            },
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [cell],
                        "graph_length": 10,
                    }
                ]
            },
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "G",
                        "source": "grid",
                        "cells": [cell],
                        "initial_values": [1, 2],
                    }
                ]
            },
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                with self.assertRaises(f.ConfigError):
                    load_config_with(patch)

    def test_graph_display_options_are_loaded(self):
        cfg = load_config_with(
            {
                "tiles": [
                    {
                        "index": 0,
                        "title": "CPU",
                        "source": "netdata",
                        "metric": "cpu",
                        "start_at_zero": False,
                        "initial_values": [10, 20.5],
                    }
                ]
            }
        )
        t = cfg["tiles"][0]
        self.assertFalse(t.start_at_zero)
        self.assertEqual(t.initial_values, [10.0, 20.5])

    def test_bad_graph_display_options_are_rejected(self):
        cases = [
            {"start_at_zero": "false"},
            {"start_at_zero": 0},
            {"initial_values": 5},
            {"initial_values": [1, "x"]},
            {"initial_values": [1, float("inf")]},
            {"initial_values": [1, 2, 3], "graph_length": 2},
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                with self.assertRaises(f.ConfigError):
                    load_config_with(
                        {
                            "tiles": [
                                {
                                    "index": 0,
                                    "title": "CPU",
                                    "source": "netdata",
                                    "metric": "cpu",
                                    **patch,
                                }
                            ]
                        }
                    )

    def test_plex_options_and_environment_token_are_loaded(self):
        with mock.patch.dict(os.environ, {"PLEX_TOKEN": "env-token"}):
            cfg = load_config_with(
                {
                    "plex_url": "http://plex.example.com:32400/",
                    "plex_insecure": True,
                    "plex_ca": "/tmp/plex.pem",
                    "tiles": [{"index": 0, "title": "PLEX", "source": "plex"}],
                }
            )
        self.assertEqual(cfg["plex_url"], "http://plex.example.com:32400")
        self.assertEqual(cfg["plex_token"], "env-token")
        self.assertTrue(cfg["plex_insecure"])
        self.assertEqual(cfg["plex_ca"], "/tmp/plex.pem")


class RenderTests(unittest.TestCase):
    def test_mount_decoding(self):
        self.assertEqual(f._mount_from_chart("disk_space./mnt/foo"), "/mnt/foo")
        self.assertEqual(f._mount_from_chart("disk_space._"), "/")
        self.assertEqual(f._mount_from_chart("disk_space._var_log"), "/var/log")

    def test_disk_includes_reserved_space(self):
        ctx = FakeCtx(
            FakeND({"disk_space.x": {"used": 50.0, "avail": 50.0, "reserved for root": 5.0}})
        )
        fields = f._render_disk(tile(source="netdata", metric="disk", chart="disk_space.x"), ctx)
        self.assertAlmostEqual(fields["value"], 50.0 / 105.0 * 100.0)

    def test_ram_uses_reported_total(self):
        ctx = FakeCtx(
            FakeND({"system.ram": {"free": 250.0, "used": 500.0, "cached": 200.0, "buffers": 50.0}}),
            ram_total_mib=1000.0,
        )
        fields = f._render_ram(tile(source="netdata", metric="ram"), ctx)
        self.assertEqual(fields["value"], 50.0)

    def test_cpu_excludes_guest_dimensions(self):
        ctx = FakeCtx(
            FakeND(
                {
                    "system.cpu": {"user": 40.0, "guest": 20.0, "guest_nice": 5.0, "system": 5.0},
                    "system.load": {"load1": 1.0},
                }
            )
        )
        fields = f._render_cpu(tile(source="netdata", metric="cpu"), ctx)
        self.assertEqual(fields["value"], 45.0)

    def test_net_honours_scale_and_unit(self):
        ctx = FakeCtx(FakeND({"system.net": {"received": 1000.0, "sent": 500.0}}))
        fields = f._render_net(tile(source="netdata", metric="net", scale=2.0, unit="Mbit/s"), ctx)
        self.assertEqual(fields["value"], 3.0)
        self.assertEqual(fields["unit"], "Mbit/s")

    def test_temp_default_thresholds_use_raw_units(self):
        ctx = FakeCtx(FakeND({"temp.x": {"temp": 80.0}}))
        fields = f._render_temp(tile(source="netdata", metric="temp", chart="temp.x", scale=1.8), ctx)
        self.assertEqual(fields["value"], 144.0)
        self.assertEqual(fields["color"], "Yellow")

    def test_hass_booleans_and_nonfinite(self):
        self.assertEqual(f._hass_to_number("true", "x"), 1.0)
        self.assertEqual(f._hass_to_number("false", "x"), 0.0)
        with self.assertRaises(f.HassError):
            f._hass_to_number("nan", "x")

    def test_steam_tile_shows_current_players(self):
        class FakeSteam:
            def current_players(self, appid):
                self.appid = appid
                return 710549

        steam = FakeSteam()
        ctx = FakeCtx(FakeND({}), steam=steam)
        fields = f._render_steam(tile(source="steam", appid=730), ctx)
        self.assertEqual(steam.appid, 730)
        self.assertEqual(fields["value"], 710549.0)
        self.assertEqual(fields["unit"], "players")
        self.assertEqual(fields["subtitle"], "Steam")
        self.assertEqual(fields["color"], "Cyan")
        self.assertIn("730", fields["secondary"])

    def test_plex_tile_shows_stream_breakdown(self):
        class FakePlex:
            def sessions(self):
                return f.PlexStatus(total=3, transcoding=1, paused=1)

        ctx = FakeCtx(FakeND({}), plex=FakePlex())
        fields = f._render_plex(tile(source="plex"), ctx)
        self.assertEqual(fields["value"], 3.0)
        self.assertEqual(fields["unit"], "streams")
        self.assertEqual(fields["subtitle"], "Plex")
        self.assertEqual(fields["color"], "Cyan")
        self.assertIn("2 direct", fields["secondary"])
        self.assertIn("1 transcode", fields["secondary"])
        self.assertIn("1 paused", fields["secondary"])

    def test_plex_tile_is_idle_when_no_streams(self):
        class FakePlex:
            def sessions(self):
                return f.PlexStatus()

        ctx = FakeCtx(FakeND({}), plex=FakePlex())
        fields = f._render_plex(tile(source="plex"), ctx)
        self.assertEqual(fields["value"], 0.0)
        self.assertEqual(fields["secondary"], "idle")
        self.assertEqual(fields["color"], "Legend")

    def test_grid_renders_cells_from_different_sources(self):
        class FakeHass:
            def state(self, entity):
                return {
                    "state": "23.456",
                    "attributes": {
                        "unit_of_measurement": "%",
                        "friendly_name": "Humidity",
                    },
                }

        ctx = FakeCtx(
            FakeND(
                {
                    "system.cpu": {"idle": 75.0},
                    "system.load": {"load1": 1.5},
                }
            )
        )
        ctx.hass = FakeHass()
        t = tile(
            source="grid",
            cells=[
                f.GridCell(title="CPU", source="netdata", metric="cpu"),
                f.GridCell(
                    title="HUM",
                    source="hass",
                    entity="sensor.humidity",
                    decimals=0,
                ),
            ],
        )
        fields = f.render_tile(t, ctx)
        self.assertEqual(len(fields["cells"]), 2)
        self.assertEqual(
            fields["cells"][0],
            {
                "title": "CPU",
                "value": "25",
                "unit": "%",
                "subTitle": "load 1.5 · 8 cores",
            },
        )
        self.assertEqual(fields["cells"][1]["title"], "HUM")
        self.assertEqual(fields["cells"][1]["value"], "23")
        self.assertEqual(fields["cells"][1]["unit"], "%")
        self.assertEqual(fields["cells"][1]["subTitle"], "Humidity")

    def test_a_broken_grid_cell_keeps_its_last_value(self):
        class FakeHass:
            fail = False

            def state(self, entity):
                if self.fail:
                    raise f.HassError("Home Assistant request failed")
                return {
                    "state": "85",
                    "attributes": {
                        "unit_of_measurement": "%",
                        "friendly_name": "Battery",
                    },
                }

        ctx = FakeCtx(FakeND({}))
        ctx.hass = FakeHass()
        cell = f.GridCell(title="BAT", source="hass", entity="sensor.battery")
        t = tile(source="grid", cells=[cell])
        first = f.render_tile(t, ctx)
        self.assertEqual(first["cells"][0]["value"], "85")

        ctx.hass.fail = True
        second = f.render_tile(t, ctx)
        self.assertEqual(second["cells"][0]["value"], "85")
        self.assertEqual(
            second["cells"][0]["subTitle"], "Home Assistant unavailable"
        )


class SteamTests(unittest.TestCase):
    def client(self, payload):
        class Fake(f.SteamClient):
            def __init__(self):
                super().__init__("http://x")
                self.calls = []

            def _get_json(self, path, params=None):
                self.calls.append((path, params))
                return payload

        return Fake()

    def test_current_players_uses_appid(self):
        client = self.client({"response": {"player_count": 1234, "result": 1}})
        self.assertEqual(client.current_players(730), 1234)
        self.assertEqual(client.calls, [(f.SteamClient.PATH, {"appid": 730})])

    def test_rejects_missing_or_invalid_counts(self):
        for payload in (
            {"response": {"result": 42}},
            {"response": {"result": 1}},
            {"response": {"player_count": -1, "result": 1}},
            {"response": {"player_count": 1.5, "result": 1}},
            {"unexpected": True},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(f.SteamError):
                    self.client(payload).current_players(730)

    def test_http_404_reports_unknown_appid(self):
        class Missing(f.SteamClient):
            def _get_json(self, path, params=None):
                raise f.SteamError("Steam HTTP 404", status=404)

        with self.assertRaisesRegex(f.SteamError, "no player count for appid 123"):
            Missing("http://x").current_players(123)


class PlexTests(unittest.TestCase):
    def client(self, payload):
        class Fake(f.PlexClient):
            def __init__(self):
                super().__init__("http://plex", "tok")
                self.calls = []

            def _get_json(self, path, params=None):
                self.calls.append((path, params))
                return payload

        return Fake()

    def test_sessions_counts_direct_transcode_and_paused(self):
        payload = {
            "MediaContainer": {
                "size": 3,
                "Metadata": [
                    {
                        "Player": {"state": "playing"},
                        "TranscodeSession": {"videoDecision": "transcode"},
                    },
                    {
                        "Player": {"state": "paused"},
                        "TranscodeSession": {"videoDecision": "copy"},
                    },
                    {"Player": {"state": "playing"}},
                ],
            }
        }
        client = self.client(payload)
        status = client.sessions()
        self.assertEqual(client.calls, [(f.PlexClient.PATH, None)])
        self.assertEqual(status.total, 3)
        self.assertEqual(status.transcoding, 2)
        self.assertEqual(status.direct, 1)
        self.assertEqual(status.paused, 1)

    def test_sessions_falls_back_to_size_without_metadata(self):
        status = self.client({"MediaContainer": {"size": 2}}).sessions()
        self.assertEqual(status.total, 2)
        self.assertEqual(status.transcoding, 0)
        self.assertEqual(status.direct, 2)
        self.assertEqual(status.paused, 0)

    def test_sessions_accepts_a_single_metadata_object(self):
        status = self.client(
            {"MediaContainer": {"Metadata": {"Player": {"state": "playing"}}}}
        ).sessions()
        self.assertEqual(status.total, 1)

    def test_rejects_unexpected_session_payloads(self):
        for payload in (
            None,
            [],
            {},
            {"MediaContainer": []},
            {"MediaContainer": {"Metadata": "nope"}},
            {"MediaContainer": {"Metadata": [1]}},
            {"MediaContainer": {"size": "many"}},
            {"MediaContainer": {"size": 1.5}},
            {"MediaContainer": {"size": -1}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(f.PlexError):
                    self.client(payload).sessions()

    def test_http_401_reports_a_rejected_token(self):
        error = urllib.error.HTTPError(
            "http://plex/status/sessions", 401, "Unauthorized", {}, io.BytesIO(b"")
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(f.PlexError, "rejected the token"):
                f.PlexClient("http://plex", "bad").sessions()

    def test_token_is_sent_as_a_header(self):
        client = f.PlexClient("http://plex/", "secret")
        self.assertEqual(client.base_url, "http://plex")
        self.assertEqual(client._headers()["X-Plex-Token"], "secret")
        self.assertEqual(client._headers()["Accept"], "application/json")


class LlamaTests(unittest.TestCase):
    def test_all_slots_are_summed(self):
        client = f.LlamaClient("http://x", "")
        previous = {
            1: ("a", 10.0, 0.0, 0.0),
            2: ("b", 20.0, 0.0, 0.0),
            3: ("c", 30.0, 0.0, 0.0),
            4: ("d", 40.0, 0.0, 0.0),
        }
        slots = [
            {"id": 1, "id_task": "a", "is_processing": True, "next_token": [{"n_decoded": 20}], "n_prompt_tokens_processed": 5},
            {"id": 2, "id_task": "b", "is_processing": True, "next_token": [{"n_decoded": 30}], "n_prompt_tokens_processed": 10},
            {"id": 3, "id_task": "c", "is_processing": True, "next_token": [{"n_decoded": 40}], "n_prompt_tokens_processed": 15},
            {"id": 4, "id_task": "d", "is_processing": False, "next_token": [{"n_decoded": 50}], "n_prompt_tokens_processed": 20},
        ]
        pp, tg, current, processing = client._slot_deltas(slots, previous, 2.0, 100.0)
        self.assertEqual((pp, tg), (15.0, 15.0))
        self.assertTrue(processing)
        self.assertEqual(len(current), 4)

    def test_llama_caches_models_metrics_and_idle_slots(self):
        class Fake(f.LlamaClient):
            def __init__(self):
                super().__init__("http://x", "")
                self.models = 0
                self.metrics_calls = 0
                self.slot_calls = 0

            def _get_json(self, path, params=None):
                if path == "/v1/models":
                    self.models += 1
                    return {"data": [{"id": "m", "status": {"value": "loaded"}}]}
                if path == "/slots":
                    self.slot_calls += 1
                    return []
                raise AssertionError(path)

            def _get_text(self, path, params=None):
                self.metrics_calls += 1
                return "llamacpp:predicted_tokens_seconds 4.0\n"

        client = Fake()
        self.assertEqual(client.loaded_model(), "m")
        self.assertEqual(client.loaded_model(), "m")
        self.assertEqual(client.models, 1)
        self.assertEqual(client.metrics("m")["llamacpp:predicted_tokens_seconds"], 4.0)
        self.assertEqual(client.metrics("m")["llamacpp:predicted_tokens_seconds"], 4.0)
        self.assertEqual(client.metrics_calls, 1)
        self.assertIsNone(client.live_rates("m"))
        self.assertIsNone(client.live_rates("m"))
        self.assertEqual(client.slot_calls, 1)

    def test_slots_uses_http_status(self):
        class Fake(f.LlamaClient):
            def _get_json(self, path, params=None):
                raise f.LlamaError("missing", status=404)

        client = Fake("http://x", "")
        self.assertIsNone(client.slots("m"))
        self.assertFalse(client._slots_supported)


class PolymathTests(unittest.TestCase):
    def setUp(self):
        self.client = f.PolymathClient(dict(f.DEFAULTS))

    def test_message_parsing_does_not_flag_titles(self):
        self.assertEqual(
            self.client._check_message({"message": "Tile 'Error rate' updated"}),
            "Tile 'Error rate' updated",
        )
        with self.assertRaises(f.ApiError):
            self.client._check_message({"error": "bad payload"})

    def test_tile_id_extraction(self):
        self.assertEqual(self.client._extract_tile_id({"id": "abc"}, ""), "abc")
        self.assertEqual(self.client._extract_tile_id({"message": "abc"}, "abc"), "abc")
        self.assertEqual(self.client._extract_tile_id({"message": "Tile created"}, "Tile created"), "")

    def test_update_sends_empty_fields_and_maps_404(self):
        calls = []
        self.client.port = 1234
        self.client._request = lambda *args, **kwargs: calls.append(args[2] if len(args) > 2 else kwargs.get("payload")) or {}
        t = tile(source="netdata", metric="custom", chart="x")
        self.client.update_tile(
            t, {"value": 0.0, "unit": "", "subtitle": "", "secondary": "", "color": "Green"}
        )
        self.assertEqual(calls[0]["unit"], "")
        self.assertEqual(calls[0]["subTitle"], "")
        self.assertEqual(calls[0]["secondarySubtitle"], "")

        def not_found(*args, **kwargs):
            raise f.ApiError(404, "missing")

        self.client._request = not_found
        with self.assertRaises(f.TileMissing):
            self.client.update_tile(t, {"value": 1.0})

    def test_grid_update_and_create_payloads(self):
        calls = []
        self.client.port = 1234
        self.client._request = (
            lambda *args, **kwargs: calls.append(args) or {"id": "grid-id"}
        )
        t = tile(
            index=2,
            source="grid",
            title="SUMMARY",
            id="grid-id",
            cells=[
                f.GridCell(title="CPU", source="netdata", metric="cpu"),
                f.GridCell(title="ROOM", source="hass", entity="sensor.room"),
            ],
        )
        cells = [
            {"title": "CPU", "value": "25", "unit": "%", "subTitle": "load 1.5"},
            {"title": "ROOM", "value": "21.5", "unit": "°C", "subTitle": "Hall"},
        ]
        self.client.update_tile(t, {"cells": cells})
        method, path, body = calls[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "/config/updateGenericTile")
        self.assertEqual(body["id"], "grid-id")
        self.assertEqual(body["gridConfig"], 2)
        self.assertEqual(body["grid1"], cells[0])
        self.assertEqual(body["grid2"], cells[1])
        self.assertNotIn("currentValue", body)

        t.id = ""
        t.applied_tile_type = None
        self.client.create_tile(t, {"cells": cells})
        method, path, body = calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/config/addGenericTile")
        self.assertEqual(body["type"], f.TILE_TYPE_GRID)
        self.assertEqual(body["index"], 2)
        self.assertEqual(body["gridConfig"], 2)
        self.assertEqual(body["grid2"]["title"], "ROOM")
        self.assertNotIn("length", body)
        self.assertEqual(t.id, "grid-id")
        self.assertEqual(t.applied_tile_type, "grid")

    def test_graph_create_sends_initial_values_and_start_at_zero(self):
        calls = []
        self.client.port = 1234
        self.client._request = (
            lambda *args, **kwargs: calls.append(args) or {"id": "graph-id"}
        )
        t = tile(
            source="netdata",
            metric="cpu",
            graph_length=3,
            start_at_zero=False,
            initial_values=[10.0, 20.0],
        )
        self.client.create_tile(
            t,
            {
                "value": 30.0,
                "unit": "%",
                "subtitle": "host",
                "secondary": "CPU",
                "color": "Green",
            },
        )
        method, path, body = calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/config/addGenericTile")
        self.assertEqual(body["type"], f.TILE_TYPE_GRAPH)
        self.assertEqual(body["length"], 3)
        self.assertEqual(body["initialValues"], [10.0, 20.0])
        self.assertFalse(body["startAtZero"])
        self.assertEqual(body["currentValue"], 30.0)
        self.assertEqual(body["unit"], "%")
        self.assertEqual(t.applied_tile_type, "graph")
        self.assertEqual(t.applied_graph_length, 3)

        seeded = tile(
            title="SEEDED",
            source="netdata",
            metric="cpu",
            graph_length=2,
            initial_values=[1.0, 2.0],
        )
        self.client.create_tile(seeded)
        body = calls[-1][2]
        self.assertEqual(body["initialValues"], [1.0, 2.0])
        self.assertEqual(body["currentValue"], 2.0)
        self.assertTrue(body["startAtZero"])

    def test_graph_update_sends_start_at_zero(self):
        calls = []
        self.client.port = 1234
        self.client._request = lambda *args, **kwargs: calls.append(args) or {}
        t = tile(source="netdata", metric="cpu", start_at_zero=False)
        self.client.update_tile(t, {"value": 1.0})
        self.assertFalse(calls[-1][2]["startAtZero"])

    def test_tile_kind_change_recreates_without_updating(self):
        t = tile(source="grid", title="SUMMARY", applied_tile_type="graph")
        calls = []
        self.client.update_tile = lambda tile, fields: calls.append("update")
        self.client.create_tile = lambda tile, fields=None: calls.append("create")
        self.assertTrue(self.client.ensure_tile(t))
        self.assertEqual(calls, ["create"])

    def test_grid_is_reclaimed_on_setup_even_with_matching_state(self):
        t = tile(
            index=1,
            title="Power",
            source="grid",
            applied_tile_type="grid",
            cells=[f.GridCell(title="Eva", source="hass", entity="sensor.eva")],
        )
        calls = []
        self.client.update_tile = lambda tile, fields: calls.append("update")
        self.client.create_tile = lambda tile, fields=None: calls.append("create")
        self.assertTrue(self.client.ensure_tile(t))
        self.assertEqual(calls, ["create"])

    def test_graph_with_legacy_state_is_not_recreated(self):
        t = tile(
            source="netdata",
            metric="cpu",
            graph_length=120,
            applied_tile_type=None,
            applied_graph_length=120,
        )
        calls = []
        self.client.update_tile = lambda tile, fields: calls.append("update")
        self.client.create_tile = lambda tile, fields=None: calls.append("create")
        self.assertFalse(self.client.ensure_tile(t))
        self.assertEqual(calls, ["update"])

    def test_ensure_tile_preserves_last_value_and_does_not_recreate_on_500(self):
        calls = []
        t = tile(
            source="netdata",
            metric="custom",
            chart="x",
            graph_length=120,
            applied_graph_length=120,
            last_fields={"value": 42.0, "unit": "%", "subtitle": "old", "secondary": "s"},
        )
        self.client.update_tile = lambda tile, fields: calls.append(fields) or (_ for _ in ()).throw(
            f.ApiError(500, "boom")
        )
        created = []
        self.client.create_tile = lambda tile, fields=None: created.append(tile)
        with self.assertRaises(f.ApiError):
            self.client.ensure_tile(t)
        self.assertEqual(calls[0]["value"], 42.0)
        self.assertEqual(calls[0]["subtitle"], "old")
        self.assertEqual(created, [])


class BridgeTests(unittest.TestCase):
    def test_per_tile_api_error_does_not_disconnect(self):
        bridge = f.Bridge(dict(f.DEFAULTS))
        bridge.ctx = FakeCtx(FakeND({"system.cpu": {"user": 10.0}, "system.load": {"load1": 1.0}}))
        bridge.connected = True
        t = tile(source="netdata", metric="cpu", poll_seconds=2.0, graph_length=120, time_unit="× 2 s")

        class Poly:
            needs_save = False

            def update_tile(self, tile, fields):
                raise f.ApiError(400, "bad body")

        bridge.poly = Poly()
        bridge._poll_tile(t)
        self.assertTrue(bridge.connected)

    def test_grid_poll_creates_with_cells_without_a_second_update(self):
        bridge = f.Bridge(dict(f.DEFAULTS))
        bridge.ctx = FakeCtx(
            FakeND(
                {
                    "system.cpu": {"idle": 75.0},
                    "system.load": {"load1": 1.5},
                }
            )
        )
        bridge.connected = True
        t = tile(
            index=3,
            title="SUMMARY",
            source="grid",
            poll_seconds=10.0,
            graph_length=120,
            time_unit="× 10 s",
            cells=[
                f.GridCell(title="CPU", source="netdata", metric="cpu"),
            ],
        )

        class Poly:
            needs_save = False

            def __init__(self):
                self.calls = []

            def update_tile(self, tile, fields):
                self.calls.append(("update", fields))
                raise f.TileMissing("gone")

            def create_tile(self, tile, fields=None):
                self.calls.append(("create", fields))

            def retry_save(self):
                pass

        bridge.poly = Poly()
        with tempfile.TemporaryDirectory() as d:
            bridge.state_path = Path(d) / "state.json"
            bridge._poll_tile(t)
        self.assertEqual([call[0] for call in bridge.poly.calls], ["update", "create"])
        cells = bridge.poly.calls[1][1]["cells"]
        self.assertEqual(cells[0]["title"], "CPU")
        self.assertEqual(cells[0]["value"], "25")
        self.assertEqual(cells[0]["unit"], "%")

    def test_state_file_records_tile_type_and_graph_length(self):
        graph = tile(
            source="netdata",
            metric="cpu",
            applied_tile_type="graph",
            applied_graph_length=120,
        )
        grid = tile(
            index=3,
            title="G",
            source="grid",
            applied_tile_type="grid",
            cells=[f.GridCell(title="CPU", source="netdata", metric="cpu")],
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            f.save_state(path, [graph, grid])
            state = json.loads(path.read_text())
        self.assertEqual(state[f.tile_type_state_key(graph)], "graph")
        self.assertEqual(state[f.tile_type_state_key(grid)], "grid")
        self.assertEqual(state[f.tile_length_state_key(graph)], "120")
        self.assertNotIn(f.tile_length_state_key(grid), state)

    def test_setup_always_saves(self):
        bridge = f.Bridge(dict(f.DEFAULTS))
        bridge.tiles = [tile(source="netdata", metric="cpu", poll_seconds=2.0, graph_length=120, time_unit="× 2 s")]

        class Poly:
            needs_save = False

            def __init__(self):
                self.saves = 0

            def connect(self):
                pass

            def setup_tiles(self, tiles):
                pass

            def save(self):
                self.saves += 1

        bridge.poly = Poly()
        with tempfile.TemporaryDirectory() as d:
            bridge.state_path = Path(d) / "state.json"
            bridge._setup()
        self.assertEqual(bridge.poly.saves, 1)


class SubtextTests(unittest.TestCase):
    class FakeHass:
        def __init__(self, state="85", unit="%"):
            self.state_value = state
            self.unit = unit
            self.fail = False

        def state(self, entity):
            if self.fail:
                raise f.HassError("Home Assistant request failed")
            return {
                "state": self.state_value,
                "attributes": {
                    "unit_of_measurement": self.unit,
                    "friendly_name": "Battery",
                },
            }

    def test_eta_projection_formats_reached_flat_and_away(self):
        history = f.SeriesHistory()
        for i in range(21):
            history.record("up", i * 5.0, 40.0 + i)
        self.assertEqual(history.eta("up", 60.0, 100, 100.0), "100% in 3m")
        self.assertEqual(history.eta("up", 100.0, 100, 100.0), "100% reached")

        down = f.SeriesHistory()
        for i in range(21):
            down.record("down", i * 5.0, 80.0 - i)
        self.assertEqual(down.eta("down", 60.0, 0, 100.0), "0% in 5m")
        self.assertEqual(down.eta("down", 0.0, 0, 100.0), "0% reached")

        flat = f.SeriesHistory()
        for i in range(6):
            flat.record("flat", i * 10.0, 50.0)
        self.assertEqual(flat.eta("flat", 50.0, 100, 50.0), "100%: flat")

        away = f.SeriesHistory()
        for i in range(6):
            away.record("away", i * 10.0, 40.0 - i)
        self.assertEqual(away.eta("away", 35.0, 100, 50.0), "100%: moving away")

        collecting = f.SeriesHistory()
        self.assertEqual(collecting.eta("none", 50.0, 100, 10.0), "100%: collecting")

    def test_hass_percent_tile_eta_replaces_default_secondary(self):
        ctx = FakeCtx(FakeND({}))
        ctx.hass = self.FakeHass(state="50")
        ctx.history = f.SeriesHistory()
        for i in range(11):
            ctx.history.record("tile:0:T", 900.0 + i * 10.0, 40.0 + i)
        with mock.patch.object(f.time, "monotonic", return_value=1000.0):
            fields = f.render_tile(
                tile(source="hass", entity="sensor.battery", eta=100), ctx
            )
        self.assertEqual(fields["value"], 50.0)
        self.assertEqual(fields["secondary"], "100% in 8m")

    def test_two_subtexts_can_read_different_sources(self):
        ctx = FakeCtx(
            FakeND({"system.cpu": {"user": 25.0}, "system.load": {"load1": 1.5}})
        )
        ctx.hass = self.FakeHass()
        t = tile(
            source="netdata",
            metric="cpu",
            subtexts=[
                f.SubtextSpec(source="netdata", slot=0, chart="system.load", dimension="load1", label="load"),
                f.SubtextSpec(source="hass", slot=1, entity="sensor.battery", label="bat"),
            ],
        )
        fields = f.render_tile(t, ctx)
        self.assertEqual(fields["value"], 25.0)
        self.assertEqual(fields["subtitle"], "load 1.5")
        self.assertEqual(fields["secondary"], "bat 85%")

    def test_text_subtexts_can_override_both_lines(self):
        ctx = FakeCtx(FakeND({"system.cpu": {"user": 10.0}, "system.load": {"load1": 1.0}}))
        t = tile(
            source="netdata",
            metric="cpu",
            subtexts=[
                f.SubtextSpec(source="text", slot=0, text="top line"),
                f.SubtextSpec(source="text", slot=1, text="bottom line"),
            ],
        )
        fields = f.render_tile(t, ctx)
        self.assertEqual(fields["subtitle"], "top line")
        self.assertEqual(fields["secondary"], "bottom line")

    def test_a_broken_subtext_source_does_not_take_the_tile_offline(self):
        ctx = FakeCtx(FakeND({"system.cpu": {"user": 10.0}, "system.load": {"load1": 1.0}}))
        ctx.hass = self.FakeHass()
        ctx.hass.fail = True
        t = tile(
            source="netdata",
            metric="cpu",
            subtexts=[None, f.SubtextSpec(source="hass", slot=1, entity="sensor.battery")],
        )
        fields = f.render_tile(t, ctx)
        self.assertEqual(fields["value"], 10.0)
        self.assertEqual(fields["secondary"], "hass unavailable")

    def test_hass_subtext_eta_records_its_own_samples(self):
        ctx = FakeCtx(FakeND({"system.cpu": {"user": 10.0}, "system.load": {"load1": 1.0}}))
        ctx.hass = self.FakeHass(state="50")
        t = tile(
            source="netdata",
            metric="cpu",
            subtexts=[
                None,
                f.SubtextSpec(source="hass", slot=1, entity="sensor.battery", eta=100),
            ],
        )
        ctx.history = f.SeriesHistory()
        key = f._subtext_history_key(t, t.subtexts[1])
        for i in range(11):
            ctx.history.record(key, 900.0 + i * 10.0, 40.0 + i)
        with mock.patch.object(f.time, "monotonic", return_value=1000.0):
            fields = f.render_tile(t, ctx)
        self.assertEqual(fields["secondary"], "100% in 8m")

    def test_eta_needs_a_percentage_unit(self):
        ctx = FakeCtx(FakeND({}))
        ctx.hass = self.FakeHass(state="50", unit="W")
        with mock.patch.object(f.time, "monotonic", return_value=1000.0):
            fields = f.render_tile(
                tile(source="hass", entity="sensor.power", eta=100), ctx
            )
        self.assertEqual(fields["secondary"], "eta needs % (unit W)")


class TlsTests(unittest.TestCase):
    def test_http_has_no_context(self):
        self.assertIsNone(f.ssl_context_for("http://example.com"))

    def test_insecure_https_context(self):
        context = f.ssl_context_for("https://example.com", insecure=True)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)


if __name__ == "__main__":
    unittest.main()
