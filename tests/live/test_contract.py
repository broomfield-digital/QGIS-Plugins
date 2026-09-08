"""A contract monitor for the NASA POWER API. Opt-in, and not a feature test.

Every other suite in this repo runs against committed fixtures, which is what
makes them fast and offline -- and also what makes them blind. A fixture is a
photograph: it stays true about the day it was taken, not about the API. This
suite is the other half. One assertion per measured fact from
``docs/POWER-API-FACTS.md``, so the day POWER changes something the failure
names which fact stopped being true.

It is deliberately **not** run by ``make test`` or ``make test-qgis``, and never
on plugin load::

    make test-live          # POWER_LIVE=1, ~20 requests, a few seconds apart

Requests are kept few and small, and spaced, because POWER is free and its docs
ask clients not to hammer it. If you are iterating on this file, run a single
class rather than the suite.

Two things are logged rather than asserted:

* **API version.** It drifts, and it drifts *per endpoint* -- at the time of
  writing daily answers v2.9.7, monthly v2.9.8 and hourly v2.10.0, from one
  capture session. Asserting a version would fail on POWER's routine
  maintenance, which is not what this suite is for.
* **Values.** POWER reprocesses. A number changing is news; it is not a bug.
"""

from __future__ import annotations

import json
import os
import time
import unittest
import urllib.error
import urllib.request
from datetime import date

from nasa_power.core.api import (
    REGIONAL_MAX_SPAN_DEGREES,
    REGIONAL_MIN_SPAN_DEGREES,
    build_dictionary_url,
    build_power_url,
)
from nasa_power.core.decode import parse_point_response
from nasa_power.core.units import to_canonical

LIVE = os.environ.get("POWER_LIVE") == "1"

#: Boulder, where every measurement in the facts document was taken.
LAT, LON = 40.02, -105.27

#: Seconds between requests. Politeness, not a measured requirement.
DELAY = 1.0

_USER_AGENT = "qgis-nasa-power/0.1.0 (contract test; fillmore@ucar.edu)"


def _get(url: str) -> tuple[int, bytes, str]:
    """GET, returning ``(status, body, content_type)``.

    An error status is a normal outcome here -- several facts under test *are*
    error responses -- so this returns rather than raises.
    """
    time.sleep(DELAY)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def _point(**kwargs) -> str:
    params = {
        "start": "2024-02-01",
        "end": "2024-02-03",
        "latitude": LAT,
        "longitude": LON,
    }
    params.update(kwargs)
    temporal = params.pop("temporal", "daily")
    names = params.pop("parameters", ["T2M"])
    return build_power_url(temporal, "point", names, **params)


@unittest.skipUnless(LIVE, "set POWER_LIVE=1 to run the live contract suite")
class EndpointShapeTests(unittest.TestCase):
    """Which endpoints exist, and how the missing one fails."""

    def test_hourly_regional_still_returns_html_not_json(self):
        # P-1. The failure mode matters as much as the failure: a JSON error
        # could be surfaced; 19 KB of markup cannot, which is why the client
        # refuses this combination locally.
        url = (
            "https://power.larc.nasa.gov/api/temporal/hourly/regional"
            "?parameters=T2M&community=RE&latitude-min=40&latitude-max=42"
            "&longitude-min=-106&longitude-max=-104&start=20240201&end=20240201"
            "&format=JSON&time-standard=UTC"
        )
        status, body, content_type = _get(url)
        self.assertEqual(status, 404)
        self.assertIn("html", content_type.lower())
        self.assertGreater(len(body), 1000)

    def test_the_other_temporal_mode_pairs_still_exist(self):
        for temporal in ("hourly", "daily", "monthly"):
            with self.subTest(temporal=temporal, mode="point"):
                status, _body, _ct = _get(_point(temporal=temporal))
                self.assertEqual(status, 200)


@unittest.skipUnless(LIVE, "set POWER_LIVE=1 to run the live contract suite")
class LimitTests(unittest.TestCase):
    """The caps and windows the request planner is built around."""

    def test_twenty_one_point_parameters_is_still_rejected(self):
        # P-2. build_power_url refuses this locally, so the URL is hand-built.
        names = ",".join(f"T2M" for _ in range(21))
        url = (
            f"https://power.larc.nasa.gov/api/temporal/daily/point?parameters={names}"
            f"&community=RE&latitude={LAT}&longitude={LON}&start=20240201&end=20240203"
            f"&format=JSON&time-standard=UTC"
        )
        status, _body, _ct = _get(url)
        self.assertEqual(status, 422)

    def test_two_regional_parameters_is_still_rejected(self):
        url = (
            "https://power.larc.nasa.gov/api/temporal/daily/regional"
            "?parameters=T2M,RH2M&community=RE&latitude-min=40&latitude-max=42"
            "&longitude-min=-106&longitude-max=-104&start=20240201&end=20240201"
            "&format=JSON&time-standard=UTC"
        )
        status, _body, _ct = _get(url)
        self.assertEqual(status, 422)

    def test_the_two_degree_minimum_and_ten_degree_maximum_still_bind(self):
        # P-3. Both sides, because the planner's tiler depends on both: a naive
        # chunker produces a sliver the minimum rejects.
        for lat_max, expected in ((41.0, 422), (42.0, 200), (52.0, 422)):
            span = lat_max - 40.0
            with self.subTest(span=span):
                url = (
                    f"https://power.larc.nasa.gov/api/temporal/daily/regional"
                    f"?parameters=T2M&community=RE&latitude-min=40&latitude-max={lat_max}"
                    f"&longitude-min=-106&longitude-max=-104&start=20240201&end=20240201"
                    f"&format=JSON&time-standard=UTC"
                )
                status, _body, _ct = _get(url)
                self.assertEqual(status, expected)
        self.assertEqual(REGIONAL_MIN_SPAN_DEGREES, 2.0)
        self.assertEqual(REGIONAL_MAX_SPAN_DEGREES, 10.0)


@unittest.skipUnless(LIVE, "set POWER_LIVE=1 to run the live contract suite")
class TimeTests(unittest.TestCase):
    """Dates, time standards, and the thirteenth month."""

    def test_monthly_still_refuses_a_full_date_and_accepts_a_year(self):
        # P-6.
        bad = (
            f"https://power.larc.nasa.gov/api/temporal/monthly/point?parameters=T2M"
            f"&community=RE&latitude={LAT}&longitude={LON}&start=20200101&end=20211231"
            f"&format=JSON&time-standard=UTC"
        )
        self.assertEqual(_get(bad)[0], 422)
        status, body, _ct = _get(_point(temporal="monthly", start="2020-01-01", end="2021-12-31"))
        self.assertEqual(status, 200)
        keys = json.loads(body)["properties"]["parameter"]["T2M"]
        # P-7: 26 keys for 24 months.
        self.assertEqual(len(keys), 26)
        self.assertIn("202013", keys)
        self.assertIn("202113", keys)

    def test_omitting_time_standard_still_gives_local_solar_time(self):
        # P-10, and the reason every request this client builds names it.
        url = (
            f"https://power.larc.nasa.gov/api/temporal/daily/point?parameters=T2M"
            f"&community=RE&latitude={LAT}&longitude={LON}&start=20240201&end=20240201"
            f"&format=JSON"
        )
        status, body, _ct = _get(url)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["header"]["time_standard"], "LST")

    def test_the_hourly_peak_still_sits_seven_hours_apart(self):
        # P-10 again, as a consequence rather than a header: the same value at
        # two different keys is what makes the trap silent.
        peaks = {}
        for standard in ("UTC", "LST"):
            status, body, _ct = _get(
                _point(
                    temporal="hourly",
                    parameters=["ALLSKY_SFC_SW_DWN"],
                    start="2024-06-01",
                    end="2024-06-01",
                    time_standard=standard,
                )
            )
            self.assertEqual(status, 200)
            series = json.loads(body)["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]
            peaks[standard] = max(series, key=series.get)
        self.assertEqual(int(peaks["UTC"][-2:]) - int(peaks["LST"][-2:]), 7)


@unittest.skipUnless(LIVE, "set POWER_LIVE=1 to run the live contract suite")
class ProvenanceTests(unittest.TestCase):
    """Where the numbers come from, and when that changes."""

    def test_the_srb_to_ceres_seam_is_still_2001_01_01(self):
        # P-26. The single fact the plugin's "CERES" labelling rests on.
        before = _get(_point(parameters=["ALLSKY_SFC_SW_DWN"], start="2000-12-30", end="2000-12-31"))
        after = _get(_point(parameters=["ALLSKY_SFC_SW_DWN"], start="2001-01-01", end="2001-01-02"))
        self.assertEqual(json.loads(before[1])["header"]["sources"], ["SRB"])
        self.assertEqual(json.loads(after[1])["header"]["sources"], ["SYN1DEG"])

    def test_sources_are_still_reported_per_request_not_per_parameter(self):
        # P-24, the reason requests are split by parent dataset.
        status, body, _ct = _get(_point(parameters=["T2M", "ALLSKY_SFC_SW_DWN"]))
        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(json.loads(body)["header"]["sources"]), ["MERRA2", "SYN1DEG"]
        )

    def test_pre_1984_radiation_is_still_dropped_from_a_200_not_refused(self):
        # P-27/P-28, and the sole justification for RECORD_START being a
        # WARNING rather than BLOCKING: POWER *accepts* this request.
        status, body, _ct = _get(
            _point(parameters=["T2M", "ALLSKY_SFC_SW_DWN"], start="1983-06-01", end="1983-06-03")
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(list(payload["properties"]["parameter"]), ["T2M"])
        self.assertTrue(payload["messages"])

    def test_radiation_alone_before_1984_is_still_refused(self):
        status, _body, _ct = _get(
            _point(parameters=["ALLSKY_SFC_SW_DWN"], start="1983-06-01", end="1983-06-03")
        )
        self.assertEqual(status, 422)


@unittest.skipUnless(LIVE, "set POWER_LIVE=1 to run the live contract suite")
class UnitsTests(unittest.TestCase):
    """Units per community, and that the conversion table still covers them."""

    def test_radiation_units_still_differ_per_community(self):
        # P-13. The fact that makes a (parameter, temporal)-keyed catalogue wrong.
        seen = {}
        for community in ("RE", "AG", "SB"):
            status, body, _ct = _get(
                _point(parameters=["ALLSKY_SFC_SW_DWN"], community=community)
            )
            self.assertEqual(status, 200)
            payload = json.loads(body)
            seen[community] = payload["parameters"]["ALLSKY_SFC_SW_DWN"]["units"]
        self.assertEqual(len(set(seen.values())), 3, seen)

    def test_all_three_communities_still_agree_after_conversion(self):
        # The physical-consequence check: whatever POWER calls them, the three
        # must describe the same irradiance.
        watts = []
        for community in ("RE", "AG", "SB"):
            status, body, _ct = _get(
                _point(parameters=["ALLSKY_SFC_SW_DWN"], community=community)
            )
            self.assertEqual(status, 200)
            payload = json.loads(body)
            units = payload["parameters"]["ALLSKY_SFC_SW_DWN"]["units"]
            first = next(iter(payload["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"].values()))
            watts.append(to_canonical(first, units, "daily"))
        self.assertLess((max(watts) - min(watts)) / max(watts), 0.001, watts)

    def test_hourly_solar_is_still_an_accumulation_not_a_rate(self):
        # P-14: Wh/m^2, not W/m^2. The difference is a factor of 3600 if the
        # conversion is written from the wrong assumption.
        status, body, _ct = _get(
            _point(temporal="hourly", parameters=["ALLSKY_SFC_SW_DWN"], start="2024-06-01", end="2024-06-01")
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["parameters"]["ALLSKY_SFC_SW_DWN"]["units"], "Wh/m^2"
        )

    def test_every_unit_the_dictionaries_serve_is_still_known(self):
        # The test that catches a NEW unit string: a parameter this client
        # cannot convert must be one it knows it cannot convert.
        from nasa_power.core.dictionary import parse_dictionary
        from nasa_power.core.units import rule_for

        unknown = {}
        for community in ("RE", "AG", "SB"):
            status, body, _ct = _get(build_dictionary_url(community, "daily"))
            self.assertEqual(status, 200)
            for info in parse_dictionary(body).values():
                if info.units and rule_for(info.units) is None:
                    unknown.setdefault(info.units, info.name)
        self.assertEqual(unknown, {}, f"units with no rule in core.units: {unknown}")


@unittest.skipUnless(LIVE, "set POWER_LIVE=1 to run the live contract suite")
class FormatTests(unittest.TestCase):
    """The wire formats the plugin depends on."""

    def test_point_json_is_still_geojson_carrying_its_own_metadata(self):
        status, body, content_type = _get(_point(parameters=["T2M", "ALLSKY_SFC_SW_DWN"]))
        self.assertEqual(status, 200)
        self.assertIn("json", content_type.lower())
        payload = json.loads(body)
        self.assertEqual(payload["type"], "Feature")
        self.assertEqual(payload["geometry"]["type"], "Point")
        header = payload["header"]
        for key in ("sources", "fill_value", "time_standard", "api"):
            self.assertIn(key, header)
        self.assertEqual(header["fill_value"], -999.0)

    def test_regional_json_is_still_a_featurecollection_of_cell_centres(self):
        url = build_power_url(
            "daily",
            "regional",
            ["T2M"],
            start="2024-02-01",
            end="2024-02-01",
            bbox={"lat_min": 40, "lat_max": 42, "lon_min": -106, "lon_max": -104},
        )
        status, body, _ct = _get(url)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertTrue(payload["features"])

    def test_regional_netcdf_still_arrives_without_a_crs(self):
        # P-18. The plugin sets EPSG:4326 itself; this is the check that the
        # reason still holds.
        from osgeo import gdal

        gdal.UseExceptions()
        url = build_power_url(
            "daily",
            "regional",
            ["T2M"],
            start="2024-02-01",
            end="2024-02-03",
            bbox={"lat_min": 40, "lat_max": 42, "lon_min": -106, "lon_max": -104},
            fmt="NETCDF",
        )
        status, body, content_type = _get(url)
        self.assertEqual(status, 200)
        self.assertIn("netcdf", content_type.lower())
        gdal.FileFromMemBuffer("/vsimem/contract.nc", body)
        try:
            dataset = gdal.Open("/vsimem/contract.nc")
            self.assertEqual(dataset.GetProjection(), "")
            self.assertEqual(dataset.RasterCount, 3)
            # P-20: the epoch is per parameter and era, never hardcoded.
            self.assertTrue(dataset.GetMetadata().get("time#units"))
            dataset = None
        finally:
            gdal.Unlink("/vsimem/contract.nc")

    def test_the_format_enum_is_still_closed(self):
        # There is no GeoJSON format -- because JSON already is one.
        url = _point().replace("format=JSON", "format=GEOJSON")
        status, body, _ct = _get(url)
        self.assertEqual(status, 422)
        self.assertIn(b"netcdf", body.lower())


@unittest.skipUnless(LIVE, "set POWER_LIVE=1 to run the live contract suite")
class DriftLog(unittest.TestCase):
    """Recorded, not asserted. Drift here is expected, not a fault."""

    def test_log_the_api_versions(self):
        versions = {}
        for temporal in ("hourly", "daily", "monthly"):
            start, end = ("2020-01-01", "2020-12-31") if temporal == "monthly" else ("2024-02-01", "2024-02-01")
            status, body, _ct = _get(_point(temporal=temporal, start=start, end=end))
            self.assertEqual(status, 200)
            api = json.loads(body)["header"]["api"]
            versions[temporal] = f"{api['name']} {api['version']}"
        print("\n  POWER API versions today:")
        for temporal, version in versions.items():
            print(f"    {temporal:9s} {version}")
        # The only assertion: they are still reported at all, since the
        # citation is built from them.
        self.assertTrue(all(versions.values()))
