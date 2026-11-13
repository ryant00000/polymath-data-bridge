import json
import ssl
import tempfile
import unittest
from pathlib import Path

import flux_netdata_bridge as f


BASE_CONFIG = {
    "netdata_url": "http://netdata.example.com:19999",
    "llama_url": "http://llama.example.com:10000",
    "ha_url": "http://homeassistant.example.com:8123",
    "tiles": [{"index": 0, "title": "CPU", "metric": "cpu"}],
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
    values = {"index": 0, "title": "T", "metric": "cpu"}
    values.update(kwargs)
    return f.TileConfig(**values)


class FakeND:
    def __init__(self, data):
        self.data = data

    def sample(self, chart):
        return self.data[chart]


class FakeCtx:
    def __init__(self, nd, ram_total_mib=1000.0):
        self.nd = nd
        self.llama = None
        self.hass = None
        self.server_name = "host"
        self.cores = 8
        self.ram_total_mib = ram_total_mib


class ConfigTests(unittest.TestCase):
    def test_rejects_bad_numbers_and_typos(self):
        cases = [
            {"warn": "high"},
            {"poll_seconds": float("nan")},
            {"graph_length": float("inf")},
            {"tiles": [{"index": 0.9, "title": "CPU", "metric": "cpu"}]},
            {"tiles": [{"index": 0, "title": "CPU", "metric": "cpu", "warn": 80}]},
            {"netdata_insecure": "false"},
            {"tile": []},
            {"tiles": [{"index": 0, "title": "CPU", "metric": "cpu", "colour": "red"}]},
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                with self.assertRaises(f.ConfigError):
                    load_config_with(patch)

    def test_accepts_discovery_mode_without_valid_tiles(self):
        cfg = load_config_with(
            {"tiles": [{"index": 99, "title": "", "metric": "nope"}]},
            validate_tiles=False,
        )
        self.assertEqual(cfg["tiles"], [])

    def test_new_options_are_loaded(self):
        cfg = load_config_with(
            {
                "llama_insecure": True,
                "llama_ca": "/tmp/llama.pem",
                "ha_insecure": True,
                "ha_ca": "/tmp/ha.pem",
                "polymath_host": "127.0.0.1",
            }
        )
        self.assertTrue(cfg["llama_insecure"])
        self.assertTrue(cfg["ha_insecure"])
        self.assertEqual(cfg["polymath_host"], "127.0.0.1")


class RenderTests(unittest.TestCase):
    def test_mount_decoding(self):
        self.assertEqual(f._mount_from_chart("disk_space./mnt/foo"), "/mnt/foo")
        self.assertEqual(f._mount_from_chart("disk_space._"), "/")
        self.assertEqual(f._mount_from_chart("disk_space._var_log"), "/var/log")

    def test_disk_includes_reserved_space(self):
        ctx = FakeCtx(
            FakeND({"disk_space.x": {"used": 50.0, "avail": 50.0, "reserved for root": 5.0}})
        )
        fields = f._render_disk(tile(metric="disk", chart="disk_space.x"), ctx)
        self.assertAlmostEqual(fields["value"], 50.0 / 105.0 * 100.0)

    def test_ram_uses_reported_total(self):
        ctx = FakeCtx(
            FakeND({"system.ram": {"free": 250.0, "used": 500.0, "cached": 200.0, "buffers": 50.0}}),
            ram_total_mib=1000.0,
        )
        fields = f._render_ram(tile(metric="ram"), ctx)
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
        fields = f._render_cpu(tile(metric="cpu"), ctx)
        self.assertEqual(fields["value"], 45.0)

    def test_net_honours_scale_and_unit(self):
        ctx = FakeCtx(FakeND({"system.net": {"received": 1000.0, "sent": 500.0}}))
        fields = f._render_net(tile(metric="net", scale=2.0, unit="Mbit/s"), ctx)
        self.assertEqual(fields["value"], 3.0)
        self.assertEqual(fields["unit"], "Mbit/s")

    def test_temp_default_thresholds_use_raw_units(self):
        ctx = FakeCtx(FakeND({"temp.x": {"temp": 80.0}}))
        fields = f._render_temp(tile(metric="temp", chart="temp.x", scale=1.8), ctx)
        self.assertEqual(fields["value"], 144.0)
        self.assertEqual(fields["color"], "Yellow")

    def test_hass_booleans_and_nonfinite(self):
        self.assertEqual(f._hass_to_number("true", "x"), 1.0)
        self.assertEqual(f._hass_to_number("false", "x"), 0.0)
        with self.assertRaises(f.HassError):
            f._hass_to_number("nan", "x")


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
        t = tile(metric="custom", chart="x")
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

    def test_ensure_tile_preserves_last_value_and_does_not_recreate_on_500(self):
        calls = []
        t = tile(
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
        self.client.create_tile = lambda tile: created.append(tile)
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
        t = tile(metric="cpu", poll_seconds=2.0, graph_length=120, time_unit="× 2 s")

        class Poly:
            needs_save = False

            def update_tile(self, tile, fields):
                raise f.ApiError(400, "bad body")

        bridge.poly = Poly()
        bridge._poll_tile(t)
        self.assertTrue(bridge.connected)

    def test_setup_always_saves(self):
        bridge = f.Bridge(dict(f.DEFAULTS))
        bridge.tiles = [tile(metric="cpu", poll_seconds=2.0, graph_length=120, time_unit="× 2 s")]

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


class TlsTests(unittest.TestCase):
    def test_http_has_no_context(self):
        self.assertIsNone(f.ssl_context_for("http://example.com"))

    def test_insecure_https_context(self):
        context = f.ssl_context_for("https://example.com", insecure=True)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)


if __name__ == "__main__":
    unittest.main()
