"""What ``nasa_power.core.api`` must never get wrong.

Three things are guarded here, in descending order of how expensive they are to
get wrong:

1. **The golden URL.** The URL *is* the cache key, so its query order is part
   of the on-disk contract. Reordering the query would not break a single
   request -- it would silently orphan every cached file. The first test in
   this file compares one byte for byte against the URL recorded in
   ``tests/fixtures/MANIFEST.json``, which is the URL the committed fixture was
   actually captured from.
2. **Both sides of every API limit**, each of which was measured against the
   live API (2 and 10 degrees, 20 and 1 parameters, the missing
   ``hourly/regional`` endpoint, year-only monthly dates). A limit tested on
   one side only is half a test: the 10-degree check has to be a strict ``>``
   or the tiler's own exactly-10-degree tiles would be rejected by the very
   function that is supposed to accept them.
3. **That a cache hit never touches the network.** POWER's docs warn that a
   client which "persists in requesting the same relative location" may be
   blocked, so politeness is a correctness property. It is asserted by passing
   a fetcher that raises if it is called at all.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

from nasa_power.core.api import (
    POINT_MAX_PARAMS,
    REGIONAL_MAX_PARAMS,
    REGIONAL_MAX_SPAN_DEGREES,
    REGIONAL_MIN_SPAN_DEGREES,
    SPAN_TOLERANCE,
    PowerRequest,
    build_dictionary_url,
    build_power_url,
    cache_path,
    count_requests,
    fetch_to_cache,
    fetch_with_retries,
    format_power_date,
    plan_requests,
    tile_bbox,
)
from nasa_power.core.errors import PowerCacheMiss, PowerHTTPError, PowerValidationError
from nasa_power.core.provenance import Family
from tests.unit.support import load_bytes, load_json

MANIFEST = load_json("MANIFEST.json")

#: The Boulder site every point fixture was captured at.
BOULDER = {"name": "Boulder", "latitude": 40.02, "longitude": -105.27}

#: 25 real daily parameter names, all of which ``family_of`` puts in the
#: meteorology family -- so family splitting cannot hide the 20-parameter
#: chunking this list exists to exercise. Taken from dict_daily_RE.json.
TWENTY_FIVE_MET_PARAMS = [
    "AIRMASS", "CDD0", "CDD10", "CDD18_3", "DISPH",
    "EVLAND", "EVPTRNS", "FROST_DAYS", "FRSEAICE", "FRSNO",
    "GDD10", "GDD13_3", "GDD15_6", "GDD4_4", "GDD7_2",
    "GWETPROF", "GWETROOT", "GWETTOP", "GWM_HEIGHT", "GWM_HEIGHT_ANOMALY",
    "HDD0", "HDD10", "HDD18_3", "IMERG_PRECLIQUID_PROB", "IMERG_PRECTOT",
]


def _bbox(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> dict:
    return {
        "lat_min": lat_min,
        "lat_max": lat_max,
        "lon_min": lon_min,
        "lon_max": lon_max,
    }


class ExplodingFetcher:
    """A fetcher that fails the test if anything asks it for bytes."""

    def fetch(self, url: str, timeout: float = 60.0) -> bytes:
        raise AssertionError(f"the network was touched for {url}")


class RecordingFetcher:
    """Return a fixed body, or raise a fixed error, and count the calls."""

    def __init__(self, body: bytes | None = None, error: Exception | None = None):
        self.body = body
        self.error = error
        self.calls: list[str] = []

    def fetch(self, url: str, timeout: float = 60.0) -> bytes:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        assert self.body is not None
        return self.body


# --------------------------------------------------------------------------- #
# The golden URL
# --------------------------------------------------------------------------- #


class GoldenUrlTest(unittest.TestCase):
    """The URL is the cache key. Byte-for-byte, or the cache is orphaned."""

    def test_daily_point_url_matches_the_captured_fixture_url(self):
        # MANIFEST records the exact URL point_daily_2param.json was fetched
        # from. If this drifts, the committed fixture no longer describes what
        # the code asks for -- and every cache entry on every user's disk is
        # unreachable.
        url = build_power_url(
            "daily",
            "point",
            ["T2M", "ALLSKY_SFC_SW_DWN"],
            start="2024-02-01",
            end="2024-02-03",
            latitude=40.02,
            longitude=-105.27,
            community="RE",
            fmt="JSON",
            time_standard="UTC",
        )
        self.assertEqual(url, MANIFEST["point_daily_2param.json"]["url"])

    def test_query_key_order_is_fixed(self):
        url = build_power_url(
            "daily",
            "point",
            ["T2M"],
            start="2024-02-01",
            end="2024-02-03",
            latitude=40.02,
            longitude=-105.27,
        )
        keys = [k for k, _ in parse_qsl(urlsplit(url).query)]
        self.assertEqual(
            keys,
            [
                "parameters",
                "community",
                "latitude",
                "longitude",
                "start",
                "end",
                "format",
                "time-standard",
            ],
        )

    def test_regional_url_matches_the_captured_fixture_url(self):
        url = build_power_url(
            "daily",
            "regional",
            ["T2M"],
            start="2024-02-01",
            end="2024-02-01",
            bbox=_bbox(40, 42, -106, -104),
            community="RE",
            fmt="JSON",
            time_standard="UTC",
        )
        self.assertEqual(url, MANIFEST["regional_daily_fc.json"]["url"])

    def test_regional_query_key_order_is_fixed(self):
        url = build_power_url(
            "daily",
            "regional",
            ["T2M"],
            start="2024-02-01",
            end="2024-02-01",
            bbox=_bbox(40, 42, -106, -104),
        )
        keys = [k for k, _ in parse_qsl(urlsplit(url).query)]
        self.assertEqual(
            keys,
            [
                "parameters",
                "community",
                "latitude-min",
                "latitude-max",
                "longitude-min",
                "longitude-max",
                "start",
                "end",
                "format",
                "time-standard",
            ],
        )

    def test_time_standard_defaults_to_utc(self):
        # POWER's own default is LST -- a ~7 h phase error at Boulder. The
        # default here must be the opposite of the API's.
        url = build_power_url(
            "hourly",
            "point",
            ["ALLSKY_SFC_SW_DWN"],
            start="2024-06-01",
            end="2024-06-01",
            latitude=40.02,
            longitude=-105.27,
        )
        self.assertIn("time-standard=UTC", url)

    def test_dictionary_url_carries_both_required_query_keys(self):
        # The bare parameter-manager path is a 422; both keys are required.
        self.assertEqual(
            build_dictionary_url("RE", "daily"),
            MANIFEST["dict_daily_RE.json"]["url"],
        )

    def test_dictionary_url_rejects_unknown_community(self):
        with self.assertRaises(PowerValidationError):
            build_dictionary_url("XX", "daily")


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #


class DateFormatTest(unittest.TestCase):
    def test_monthly_collapses_to_a_bare_year(self):
        # Verified: YYYYMMDD on the monthly endpoint is a 422 ("Please provide
        # a correct start date formatting").
        self.assertEqual(format_power_date("2020-06-15", "monthly"), "2020")

    def test_daily_and_hourly_use_yyyymmdd(self):
        self.assertEqual(format_power_date("2020-06-05", "daily"), "20200605")
        self.assertEqual(format_power_date("2020-06-05", "hourly"), "20200605")

    def test_monthly_url_carries_year_only(self):
        url = build_power_url(
            "monthly",
            "point",
            ["T2M"],
            start="2020-01-01",
            end="2021-12-31",
            latitude=40.02,
            longitude=-105.27,
        )
        self.assertEqual(url, MANIFEST["point_monthly_yyyy13.json"]["url"])

    def test_unreadable_date_raises(self):
        with self.assertRaises(PowerValidationError):
            format_power_date("01/02/2024", "daily")


# --------------------------------------------------------------------------- #
# Limits: both sides of every one
# --------------------------------------------------------------------------- #


class RegionalSpanBoundaryTest(unittest.TestCase):
    """2.0 and 10.0 degrees are both *inclusive*, and both were measured."""

    def _regional(self, bbox):
        return build_power_url(
            "daily", "regional", ["T2M"], start="2024-02-01", end="2024-02-01", bbox=bbox
        )

    def test_exactly_two_degrees_is_accepted(self):
        self.assertIn("latitude-min=40", self._regional(_bbox(40.0, 42.0, -106.0, -104.0)))

    def test_one_point_nine_nine_degrees_is_rejected(self):
        # Verified: 1.9 degrees -> 422 "Please provide at least a 2 degree
        # range in latitude; otherwise use the point endpoint."
        with self.assertRaises(PowerValidationError):
            self._regional(_bbox(40.0, 41.99, -106.0, -104.0))

    def test_the_minimum_is_enforced_on_longitude_too(self):
        with self.assertRaises(PowerValidationError):
            self._regional(_bbox(40.0, 42.0, -106.0, -104.01))

    def test_exactly_ten_degrees_is_accepted(self):
        # This is why the maximum check is a strict '>': _split_span emits
        # exactly-10.0 tiles, and the API accepts them.
        url = self._regional(_bbox(30.0, 40.0, -110.0, -100.0))
        self.assertIn("latitude-max=40", url)

    def test_ten_point_zero_one_degrees_is_rejected(self):
        # Verified: 12 degrees -> 422 "Please provide a maximum of 10 degree
        # range in latitude." 10.01 is the first rejected value we can express.
        with self.assertRaises(PowerValidationError):
            self._regional(_bbox(30.0, 40.01, -110.0, -100.0))

    def test_the_maximum_is_enforced_on_longitude_too(self):
        with self.assertRaises(PowerValidationError):
            self._regional(_bbox(30.0, 40.0, -110.0, -99.99))

    def test_missing_bbox_key_is_named(self):
        with self.assertRaises(PowerValidationError) as caught:
            self._regional({"lat_min": 40.0, "lat_max": 42.0, "lon_min": -106.0})
        self.assertIn("lon_max", str(caught.exception))

    def test_regional_without_bbox_raises(self):
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily", "regional", ["T2M"], start="2024-02-01", end="2024-02-01"
            )


class ParameterCountBoundaryTest(unittest.TestCase):
    def test_twenty_parameters_pass_on_point(self):
        params = [f"P{i:02d}" for i in range(POINT_MAX_PARAMS)]
        url = build_power_url(
            "daily",
            "point",
            params,
            start="2024-02-01",
            end="2024-02-01",
            latitude=40.02,
            longitude=-105.27,
        )
        self.assertIn("P19", url)

    def test_twenty_one_parameters_are_rejected_on_point(self):
        # Verified: 21 parameters on the point endpoint -> 422.
        params = [f"P{i:02d}" for i in range(POINT_MAX_PARAMS + 1)]
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily",
                "point",
                params,
                start="2024-02-01",
                end="2024-02-01",
                latitude=40.02,
                longitude=-105.27,
            )

    def test_one_parameter_passes_on_regional(self):
        self.assertEqual(REGIONAL_MAX_PARAMS, 1)
        url = build_power_url(
            "daily",
            "regional",
            ["T2M"],
            start="2024-02-01",
            end="2024-02-01",
            bbox=_bbox(40, 42, -106, -104),
        )
        self.assertIn("parameters=T2M", url)

    def test_two_parameters_are_rejected_on_regional(self):
        # Verified: 2 parameters -> 422 "A maximum of 1 parameters are can
        # currently be requested" (sic).
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily",
                "regional",
                ["T2M", "PRECTOTCORR"],
                start="2024-02-01",
                end="2024-02-01",
                bbox=_bbox(40, 42, -106, -104),
            )

    def test_no_parameters_at_all_is_rejected(self):
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily",
                "point",
                [],
                start="2024-02-01",
                end="2024-02-01",
                latitude=40.02,
                longitude=-105.27,
            )


class EndpointAndEnumTest(unittest.TestCase):
    def test_hourly_regional_is_refused_locally(self):
        # Verified: this combination returns a 19 KB text/html 404, not a JSON
        # API error (tests/fixtures/error_404_hourly_regional.html), so the
        # user would otherwise see a wall of markup.
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "hourly",
                "regional",
                ["T2M"],
                start="2024-02-01",
                end="2024-02-01",
                bbox=_bbox(40, 42, -106, -104),
            )

    def test_the_other_five_temporal_mode_pairs_all_build(self):
        for temporal in ("hourly", "daily", "monthly"):
            for mode in ("point", "regional"):
                if (temporal, mode) == ("hourly", "regional"):
                    continue
                with self.subTest(temporal=temporal, mode=mode):
                    build_power_url(
                        temporal,
                        mode,
                        ["T2M"],
                        start="2024-02-01",
                        end="2024-02-01",
                        latitude=40.02,
                        longitude=-105.27,
                        bbox=_bbox(40, 42, -106, -104),
                    )

    def test_unknown_temporal_raises(self):
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "weekly",
                "point",
                ["T2M"],
                start="2024-02-01",
                end="2024-02-01",
                latitude=40.02,
                longitude=-105.27,
            )

    def test_unknown_mode_raises(self):
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily",
                "raster",
                ["T2M"],
                start="2024-02-01",
                end="2024-02-01",
                latitude=40.02,
                longitude=-105.27,
            )

    def test_unknown_format_raises(self):
        # GEOJSON looks plausible and is not in the enum; the API 422s on it.
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily",
                "point",
                ["T2M"],
                start="2024-02-01",
                end="2024-02-01",
                latitude=40.02,
                longitude=-105.27,
                fmt="GEOJSON",
            )

    def test_format_is_case_insensitive_and_normalised(self):
        url = build_power_url(
            "daily",
            "point",
            ["T2M"],
            start="2024-02-01",
            end="2024-02-01",
            latitude=40.02,
            longitude=-105.27,
            fmt="netcdf",
        )
        self.assertIn("format=NETCDF", url)


class CoordinateTest(unittest.TestCase):
    def test_longitude_outside_180_raises(self):
        # The reprojection trap: a bbox taken from a Web Mercator project comes
        # through in metres, and 1.17e7 is not a longitude.
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily",
                "point",
                ["T2M"],
                start="2024-02-01",
                end="2024-02-01",
                latitude=40.02,
                longitude=-11724000.0,
            )

    def test_longitude_exactly_180_is_accepted(self):
        url = build_power_url(
            "daily",
            "point",
            ["T2M"],
            start="2024-02-01",
            end="2024-02-01",
            latitude=0.0,
            longitude=180.0,
        )
        self.assertIn("longitude=180.0", url)

    def test_latitude_outside_90_raises(self):
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily",
                "point",
                ["T2M"],
                start="2024-02-01",
                end="2024-02-01",
                latitude=4900000.0,
                longitude=-105.27,
            )

    def test_point_without_coordinates_raises(self):
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily", "point", ["T2M"], start="2024-02-01", end="2024-02-01"
            )


# --------------------------------------------------------------------------- #
# Tiling
# --------------------------------------------------------------------------- #


class TileBboxTest(unittest.TestCase):
    #: Western US: 20 degrees of latitude by 25 of longitude.
    CONTINENTAL = _bbox(30.0, 50.0, -125.0, -100.0)

    def test_continental_bbox_makes_exactly_six_tiles(self):
        # 20 deg lat -> 2 pieces of 10.0; 25 deg lon -> 3 pieces of 8.333.
        self.assertEqual(len(tile_bbox(self.CONTINENTAL)), 6)

    def test_tiles_are_contiguous_on_both_axes(self):
        tiles = tile_bbox(self.CONTINENTAL)
        for lo_key, hi_key in (("lat_min", "lat_max"), ("lon_min", "lon_max")):
            edges = sorted({(t[lo_key], t[hi_key]) for t in tiles})
            for (prev_lo, prev_hi), (next_lo, next_hi) in zip(edges, edges[1:]):
                with self.subTest(axis=lo_key, prev=prev_hi, next=next_lo):
                    # Exact equality, not almost-equal: the edges come from one
                    # shared list, so a gap here means the tiler was rewritten.
                    self.assertEqual(next_lo, prev_hi)

    def test_tile_union_reproduces_the_requested_bbox(self):
        tiles = tile_bbox(self.CONTINENTAL)
        self.assertEqual(min(t["lat_min"] for t in tiles), self.CONTINENTAL["lat_min"])
        self.assertEqual(max(t["lat_max"] for t in tiles), self.CONTINENTAL["lat_max"])
        self.assertEqual(min(t["lon_min"] for t in tiles), self.CONTINENTAL["lon_min"])
        self.assertEqual(max(t["lon_max"] for t in tiles), self.CONTINENTAL["lon_max"])

    def test_no_tile_is_below_the_two_degree_minimum(self):
        for tile in tile_bbox(self.CONTINENTAL):
            with self.subTest(tile=tile):
                self.assertGreaterEqual(
                    tile["lat_max"] - tile["lat_min"], REGIONAL_MIN_SPAN_DEGREES
                )
                self.assertGreaterEqual(
                    tile["lon_max"] - tile["lon_min"], REGIONAL_MIN_SPAN_DEGREES
                )

    def test_twenty_one_degrees_never_produces_a_one_degree_sliver(self):
        # Naive chunking gives 10 + 10 + 1, and that 1-degree tile is itself a
        # 422. The even split gives 3 x 7.
        tiles = tile_bbox(_bbox(30.0, 51.0, -106.0, -104.0))
        spans = sorted(round(t["lat_max"] - t["lat_min"], 9) for t in tiles)
        self.assertEqual(spans, [7.0, 7.0, 7.0])

    def test_a_five_by_five_bbox_is_a_single_tile(self):
        self.assertEqual(tile_bbox(_bbox(38.0, 43.0, -108.0, -103.0)), [
            _bbox(38.0, 43.0, -108.0, -103.0)
        ])

    def test_every_emitted_tile_is_accepted_by_the_url_builder(self):
        # The coupling that matters: the tiler and the validator must agree at
        # exactly 10.0 degrees, or a tiled continental fetch fails locally
        # before it ever reaches the API.
        for bbox in (self.CONTINENTAL, _bbox(30.0, 51.0, -106.0, -104.0), _bbox(0.0, 10.0, 0.0, 10.0)):
            for tile in tile_bbox(bbox):
                with self.subTest(tile=tile):
                    build_power_url(
                        "daily",
                        "regional",
                        ["T2M"],
                        start="2024-02-01",
                        end="2024-02-01",
                        bbox=tile,
                    )

    def test_the_tiler_and_the_validator_agree_at_offset_origins_too(self):
        # Regression, and the reason the span checks carry a tolerance.
        #
        # The three bboxes above all start on a round multiple of ten, so their
        # tile edges divide exactly and the drift never appears. Off a round
        # origin it does: tiling latitude -29.8..50.2 emits a 30.2..40.2 tile
        # whose span subtracts to 10.000000000000004, which an exact ``>``
        # comparison refused -- with the message "accepts at most a 10 degree
        # range in latitude; got 10", a refusal whose own text shows the value
        # passing. count_requests promised 8 requests and plan_requests raised.
        #
        # The sweep is over origins deliberately chosen off the decimal grid.
        for lat_min in (-29.8, -15.8, 0.1, 7.3, 22.7, 30.2):
            for span in (20.0, 21.0, 33.3, 80.0):
                if lat_min + span > 90.0:
                    continue
                bbox = _bbox(lat_min, lat_min + span, 0.0, 2.0)
                with self.subTest(lat_min=lat_min, span=span):
                    tiles = tile_bbox(bbox)
                    for tile in tiles:
                        build_power_url(
                            "daily",
                            "regional",
                            ["T2M"],
                            start="2024-02-01",
                            end="2024-02-01",
                            bbox=tile,
                        )
                    # The badge and the fetch must not disagree either.
                    self.assertEqual(
                        count_requests("daily", "regional", ["T2M"], bbox=bbox),
                        len(
                            plan_requests(
                                "daily",
                                "regional",
                                ["T2M"],
                                start="2024-02-01",
                                end="2024-02-01",
                                bbox=bbox,
                            )
                        ),
                    )

    def test_no_tile_exceeds_the_ten_degree_maximum(self):
        # Compared at the tolerance the module itself uses: an equal split in
        # binary floating point can land a hair over 10.0, and the API accepts
        # that. A tile a whole degree over would still fail this.
        for bbox in (
            _bbox(-60.0, 60.0, -180.0, 180.0),
            _bbox(-29.8, 50.2, -105.3, -83.3),
        ):
            for tile in tile_bbox(bbox):
                with self.subTest(tile=tile):
                    self.assertLessEqual(
                        tile["lat_max"] - tile["lat_min"],
                        REGIONAL_MAX_SPAN_DEGREES + SPAN_TOLERANCE,
                    )
                    self.assertLessEqual(
                        tile["lon_max"] - tile["lon_min"],
                        REGIONAL_MAX_SPAN_DEGREES + SPAN_TOLERANCE,
                    )

    def test_an_exactly_two_degree_extent_is_accepted_wherever_it_sits(self):
        # The minimum side of the same drift, and the one a user meets first:
        # 0.01..2.01 is an ordinary extent, but the subtraction yields
        # 1.9999999999999998, and an exact ``<`` refused it as "requires at
        # least a 2 degree range in latitude; got 2". Every origin below is a
        # real pair whose span subtracts to just under 2.0.
        for lat_min in (0.002, 0.006, 0.01, 30.002, 30.005, 30.008):
            with self.subTest(lat_min=lat_min):
                span = (lat_min + 2.0) - lat_min
                self.assertLess(span, 2.0, "origin no longer drifts; pick another")
                build_power_url(
                    "daily",
                    "regional",
                    ["T2M"],
                    start="2024-02-01",
                    end="2024-02-01",
                    bbox=_bbox(lat_min, lat_min + 2.0, 0.0, 3.0),
                )

    def test_the_tolerance_is_far_too_small_to_admit_a_real_over_span(self):
        # The tolerance absorbs ~1e-14 of arithmetic drift, not a user's box.
        # 1e-9 degrees is about 0.1 mm on the ground.
        self.assertLess(SPAN_TOLERANCE, 1e-6)
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily",
                "regional",
                ["T2M"],
                start="2024-02-01",
                end="2024-02-01",
                bbox=_bbox(0.0, 10.0001, 0.0, 3.0),
            )
        with self.assertRaises(PowerValidationError):
            build_power_url(
                "daily",
                "regional",
                ["T2M"],
                start="2024-02-01",
                end="2024-02-01",
                bbox=_bbox(0.0, 1.9999, 0.0, 3.0),
            )

    def test_a_bbox_smaller_than_the_minimum_cannot_be_tiled(self):
        # No amount of tiling rescues a box the point endpoint should serve.
        with self.assertRaises(PowerValidationError):
            tile_bbox(_bbox(40.0, 41.0, -106.0, -104.0))


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


class PlanRequestsTest(unittest.TestCase):
    def test_twenty_five_parameters_split_into_twenty_and_five(self):
        plan = plan_requests(
            "daily",
            "point",
            TWENTY_FIVE_MET_PARAMS,
            start="2024-02-01",
            end="2024-02-03",
            sites=[BOULDER],
        )
        self.assertEqual([len(r.params) for r in plan], [20, 5])
        # Every parameter survives exactly once, in the order asked for. A
        # chunker that dropped or duplicated one would still pass a count test.
        flat = [p for r in plan for p in r.params]
        self.assertEqual(flat, TWENTY_FIVE_MET_PARAMS)

    def test_mixed_families_become_separate_attributable_requests(self):
        # header.sources is per request: asking for both at once returns
        # ['MERRA2','SYN1DEG'] and neither value is attributable (that is
        # exactly what point_daily_2param.json shows).
        plan = plan_requests(
            "daily",
            "point",
            ["T2M", "ALLSKY_SFC_SW_DWN"],
            start="2024-02-01",
            end="2024-02-03",
            sites=[BOULDER],
        )
        self.assertEqual(len(plan), 2)
        by_family = {r.family: r.params for r in plan}
        self.assertEqual(by_family[Family.METEOROLOGY], ("T2M",))
        self.assertEqual(by_family[Family.RADIATION], ("ALLSKY_SFC_SW_DWN",))

    def test_split_by_family_false_yields_the_captured_mixed_request(self):
        # And that single request is byte-for-byte the URL the ambiguous
        # fixture was captured from.
        plan = plan_requests(
            "daily",
            "point",
            ["T2M", "ALLSKY_SFC_SW_DWN"],
            start="2024-02-01",
            end="2024-02-03",
            sites=[BOULDER],
            split_by_family=False,
        )
        self.assertEqual(len(plan), 1)
        self.assertIsNone(plan[0].family)
        self.assertEqual(plan[0].url, MANIFEST["point_daily_2param.json"]["url"])

    def test_point_requests_carry_the_site_name_the_response_lacks(self):
        # The point response has no site dimension -- it comes back as
        # (time, lat=1, lon=1) -- so the label has to travel on the request.
        plan = plan_requests(
            "daily",
            "point",
            ["T2M"],
            start="2024-02-01",
            end="2024-02-03",
            sites=[BOULDER, {"name": "Denver", "latitude": 39.74, "longitude": -104.98}],
        )
        self.assertEqual([r.site for r in plan], ["Boulder", "Denver"])
        self.assertEqual([r.latitude for r in plan], [40.02, 39.74])

    def test_regional_fans_out_over_tiles_and_parameters(self):
        plan = plan_requests(
            "daily",
            "regional",
            ["T2M", "ALLSKY_SFC_SW_DWN"],
            start="2024-02-01",
            end="2024-02-03",
            bbox=_bbox(30.0, 50.0, -125.0, -100.0),
            fmt="NETCDF",
        )
        self.assertEqual(len(plan), 12)  # 6 tiles x 2 parameters
        self.assertTrue(all(len(r.params) == 1 for r in plan))
        self.assertTrue(all(r.site is None for r in plan))
        self.assertEqual(
            {r.family for r in plan}, {Family.METEOROLOGY, Family.RADIATION}
        )
        # Every tile appears once per parameter, and the bbox travels with the
        # request so the caller need not re-parse the URL.
        self.assertEqual(len({(r.params[0], tuple(sorted(r.bbox.items()))) for r in plan}), 12)

    def test_regional_urls_are_all_distinct(self):
        plan = plan_requests(
            "daily",
            "regional",
            ["T2M"],
            start="2024-02-01",
            end="2024-02-03",
            bbox=_bbox(30.0, 50.0, -125.0, -100.0),
            fmt="NETCDF",
        )
        self.assertEqual(len({r.url for r in plan}), len(plan))

    def test_point_mode_without_sites_raises(self):
        with self.assertRaises(PowerValidationError):
            plan_requests(
                "daily", "point", ["T2M"], start="2024-02-01", end="2024-02-03"
            )

    def test_a_site_missing_coordinates_raises(self):
        with self.assertRaises(PowerValidationError):
            plan_requests(
                "daily",
                "point",
                ["T2M"],
                start="2024-02-01",
                end="2024-02-03",
                sites=[{"name": "nowhere"}],
            )

    def test_regional_mode_without_bbox_raises(self):
        with self.assertRaises(PowerValidationError):
            plan_requests(
                "daily", "regional", ["T2M"], start="2024-02-01", end="2024-02-03"
            )


class CountRequestsTest(unittest.TestCase):
    """The dock's "N requests" badge must not lie about the cost."""

    SHAPES = [
        dict(mode="point", params=TWENTY_FIVE_MET_PARAMS, sites=[BOULDER]),
        dict(
            mode="point",
            params=["T2M", "ALLSKY_SFC_SW_DWN"],
            sites=[BOULDER, {"latitude": 39.74, "longitude": -104.98}, {"latitude": 0.0, "longitude": 0.0}],
        ),
        dict(
            mode="point",
            params=["T2M", "ALLSKY_SFC_SW_DWN"],
            sites=[BOULDER, {"latitude": 39.74, "longitude": -104.98}],
            split_by_family=False,
        ),
        dict(mode="point", params=TWENTY_FIVE_MET_PARAMS + ["ALLSKY_SFC_SW_DWN"], sites=[BOULDER]),
        dict(mode="regional", params=["T2M"], bbox=_bbox(40.0, 42.0, -106.0, -104.0)),
        dict(
            mode="regional",
            params=["T2M", "ALLSKY_SFC_SW_DWN"],
            bbox=_bbox(30.0, 50.0, -125.0, -100.0),
        ),
    ]

    def test_count_agrees_with_the_plan_it_predicts(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                planned = plan_requests(
                    "daily", start="2024-02-01", end="2024-02-03", **shape
                )
                self.assertEqual(
                    count_requests("daily", **shape), len(planned)
                )

    def test_a_continental_two_parameter_ask_costs_twelve_requests(self):
        self.assertEqual(
            count_requests(
                "daily",
                "regional",
                ["T2M", "ALLSKY_SFC_SW_DWN"],
                bbox=_bbox(30.0, 50.0, -125.0, -100.0),
            ),
            12,
        )


# --------------------------------------------------------------------------- #
# Cache paths
# --------------------------------------------------------------------------- #


def _request(**over) -> PowerRequest:
    base = dict(
        url="https://power.larc.nasa.gov/api/temporal/daily/point?parameters=T2M",
        temporal="daily",
        mode="point",
        params=("T2M",),
        community="RE",
        start="20240201",
        end="20240203",
        fmt="JSON",
        site="Boulder",
        latitude=40.02,
        longitude=-105.27,
    )
    base.update(over)
    return PowerRequest(**base)


class CachePathTest(unittest.TestCase):
    def test_suffix_follows_the_requested_format(self):
        # The DAVINCI bug: .nc was hardcoded, so a JSON point response landed
        # under a NetCDF name and nothing on disk could open it.
        for fmt, suffix in (
            ("JSON", ".json"),
            ("NETCDF", ".nc"),
            ("CSV", ".csv"),
            ("XARRAY", ".json"),
            ("ASCII", ".txt"),
        ):
            with self.subTest(fmt=fmt):
                path = cache_path("/cache", _request(fmt=fmt))
                self.assertEqual(path.suffix, suffix)

    def test_a_changed_url_changes_the_path(self):
        a = cache_path("/cache", _request(url="https://power.larc.nasa.gov/a"))
        b = cache_path("/cache", _request(url="https://power.larc.nasa.gov/b"))
        self.assertNotEqual(a, b)

    def test_the_same_url_gives_the_same_path(self):
        self.assertEqual(cache_path("/cache", _request()), cache_path("/cache", _request()))

    def test_layout_is_temporal_community_mode_name(self):
        path = cache_path("/cache", _request(temporal="monthly", community="AG", mode="point"))
        self.assertEqual(path.parts[-4:-1], ("monthly", "AG", "point"))
        self.assertEqual(path.parent.parent.parent.parent, Path("/cache"))

    def test_regional_entries_are_labelled_by_parameter(self):
        path = cache_path(
            "/cache",
            _request(mode="regional", site=None, params=("ALLSKY_SFC_SW_DWN",), fmt="NETCDF"),
        )
        self.assertTrue(path.name.startswith("allsky-sfc-sw-dwn-"))
        self.assertTrue(path.name.endswith(".nc"))

    def test_the_window_is_visible_in_the_filename(self):
        # The hash makes the path unique; the readable part makes the cache
        # directory answerable by eye.
        self.assertIn("20240201-20240203", cache_path("/cache", _request()).name)


# --------------------------------------------------------------------------- #
# fetch_to_cache
# --------------------------------------------------------------------------- #


class FetchToCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.request = _request()

    def _prime(self, body: bytes) -> Path:
        path = cache_path(self.cache, self.request)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_a_hit_never_calls_the_fetcher(self):
        # POWER's docs warn that a client which "persists in requesting the
        # same relative location" may be blocked. This is that claim, tested.
        primed = self._prime(b"{}")
        path, was_cached = fetch_to_cache(self.request, self.cache, ExplodingFetcher())
        self.assertTrue(was_cached)
        self.assertEqual(path, primed)

    def test_a_miss_writes_the_body_and_reports_not_cached(self):
        body = load_bytes("point_daily_2param.json")
        fetcher = RecordingFetcher(body=body)
        path, was_cached = fetch_to_cache(self.request, self.cache, fetcher)
        self.assertFalse(was_cached)
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(fetcher.calls, [self.request.url])

    def test_the_second_call_is_a_hit(self):
        fetcher = RecordingFetcher(body=b"{}")
        fetch_to_cache(self.request, self.cache, fetcher)
        _, was_cached = fetch_to_cache(self.request, self.cache, fetcher)
        self.assertTrue(was_cached)
        self.assertEqual(len(fetcher.calls), 1)

    def test_a_failed_fetch_leaves_no_file_and_no_partial(self):
        # A truncated file in the cache is permanent: hits never re-fetch.
        error = PowerHTTPError(422, load_bytes("error_422_bbox.json").decode(), self.request.url)
        with self.assertRaises(PowerHTTPError):
            fetch_to_cache(self.request, self.cache, RecordingFetcher(error=error))
        self.assertFalse(cache_path(self.cache, self.request).exists())
        self.assertEqual(list(self.cache.rglob("*.partial")), [])
        self.assertEqual([p for p in self.cache.rglob("*") if p.is_file()], [])

    def test_a_failed_rename_removes_the_partial(self):
        # The write is temp-file-then-rename; if the rename dies the temp file
        # must not survive to be mistaken for a cache entry later.
        fetcher = RecordingFetcher(body=b"{}")
        with mock.patch.object(Path, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fetch_to_cache(self.request, self.cache, fetcher)
        self.assertEqual(list(self.cache.rglob("*.partial")), [])
        self.assertFalse(cache_path(self.cache, self.request).exists())

    def test_offline_on_a_miss_raises_cache_miss(self):
        with self.assertRaises(PowerCacheMiss) as caught:
            fetch_to_cache(self.request, self.cache, ExplodingFetcher(), offline=True)
        # The URL has to be in the message: it is the only way a user can go
        # and fetch the missing entry by hand.
        self.assertIn(self.request.url, str(caught.exception))

    def test_offline_on_a_hit_still_returns_the_file(self):
        primed = self._prime(b"{}")
        path, was_cached = fetch_to_cache(
            self.request, self.cache, ExplodingFetcher(), offline=True
        )
        self.assertTrue(was_cached)
        self.assertEqual(path, primed)

    def test_force_refetches_over_an_existing_entry(self):
        self._prime(b"stale")
        fetcher = RecordingFetcher(body=b"fresh")
        path, was_cached = fetch_to_cache(self.request, self.cache, fetcher, force=True)
        self.assertFalse(was_cached)
        self.assertEqual(path.read_bytes(), b"fresh")
        self.assertEqual(len(fetcher.calls), 1)

    def test_a_miss_without_a_fetcher_raises_rather_than_returning_nothing(self):
        with self.assertRaises(PowerValidationError):
            fetch_to_cache(self.request, self.cache, None)


# --------------------------------------------------------------------------- #
# Retries
# --------------------------------------------------------------------------- #


class RetryTest(unittest.TestCase):
    URL = "https://power.larc.nasa.gov/api/temporal/daily/point?parameters=T2M"

    def setUp(self):
        # fetch_with_retries logs a warning per retry. Left alone it prints
        # mid-run, which trains the reader to scroll past exactly the kind of
        # line a real failure would produce. Capturing it also keeps the
        # "operator can see the backoff happening" behaviour under test.
        self._logs = self.assertLogs("nasa_power.core.api", level="WARNING")
        self.captured = self._logs.__enter__()
        self.addCleanup(self._suppress_no_logs_error)

    def _suppress_no_logs_error(self):
        # assertLogs fails if nothing was logged, but several tests here
        # deliberately never retry; only propagate a genuine exception.
        try:
            self._logs.__exit__(None, None, None)
        except AssertionError:
            pass

    def test_429_is_retried_to_max_tries_then_raised(self):
        fetcher = RecordingFetcher(error=PowerHTTPError(429, "slow down", self.URL))
        slept: list[float] = []
        with self.assertRaises(PowerHTTPError) as caught:
            fetch_with_retries(
                self.URL, fetcher, max_tries=3, sleep=slept.append
            )
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(fetcher.calls), 3)
        # Exponential, and no sleep after the final failure.
        self.assertEqual(slept, [1.0, 2.0])

    def test_five_hundreds_are_retried_too(self):
        fetcher = RecordingFetcher(error=PowerHTTPError(503, "", self.URL))
        slept: list[float] = []
        with self.assertRaises(PowerHTTPError):
            fetch_with_retries(self.URL, fetcher, max_tries=4, sleep=slept.append)
        self.assertEqual(len(fetcher.calls), 4)
        self.assertEqual(slept, [1.0, 2.0, 4.0])

    def test_max_tries_of_one_never_sleeps(self):
        fetcher = RecordingFetcher(error=PowerHTTPError(429, "", self.URL))
        slept: list[float] = []
        with self.assertRaises(PowerHTTPError):
            fetch_with_retries(self.URL, fetcher, max_tries=1, sleep=slept.append)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(slept, [])

    def test_a_422_is_fetched_exactly_once(self):
        # A validation failure fails identically forever; retrying it only
        # burns goodwill against a free API.
        body = load_bytes("error_422_bbox.json").decode()
        fetcher = RecordingFetcher(error=PowerHTTPError(422, body, self.URL))
        slept: list[float] = []
        with self.assertRaises(PowerHTTPError) as caught:
            fetch_with_retries(self.URL, fetcher, max_tries=3, sleep=slept.append)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(slept, [])
        message = str(caught.exception)
        # Both halves are load-bearing: POWER names the field it rejected, and
        # without the URL beside it the user cannot tell which of six tiled
        # requests was the malformed one.
        self.assertIn(
            "Please provide at least a 2 degree range in latitude", message
        )
        self.assertIn(self.URL, message)

    def test_a_success_after_a_retryable_failure_is_returned(self):
        class Flaky:
            def __init__(self):
                self.calls = 0

            def fetch(self, url, timeout=60.0):
                self.calls += 1
                if self.calls == 1:
                    raise PowerHTTPError(503, "", url)
                return b"ok"

        fetcher = Flaky()
        slept: list[float] = []
        self.assertEqual(
            fetch_with_retries(self.URL, fetcher, max_tries=3, sleep=slept.append), b"ok"
        )
        self.assertEqual(fetcher.calls, 2)
        self.assertEqual(slept, [1.0])


if __name__ == "__main__":
    unittest.main()
