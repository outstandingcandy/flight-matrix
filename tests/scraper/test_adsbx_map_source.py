"""Tests for `ADSBxMapTaskSource` and the region grid it cycles through.

`ADSBX_REGIONS` is a coverage claim, and a wrong one fails silently in the worst
possible way: a window that does not reach its neighbour leaves a band of
airspace that is simply never scraped, and every region still reports thousands
of aircraft, so nothing looks broken. The grid is therefore checked here against
the measured single-request extent it was sized from — latitude -6..73 and
longitude -81..101 at zoom 3, from a scrape centred on (50, 10).

Latitude is checked in Mercator y, not in degrees. A window's reach in degrees
depends on where it is centred, so the equator seam cannot be verified by
comparing latitudes: the first version of this grid spaced its rows evenly in
degrees and left a 6-degree band across the equator uncovered, which is what
these tests caught.

A later probe of all six windows bore the model out: both windows centred on
longitude 0 returned exactly -91.4..91.3, and the northern rows reached 0.80-0.87
of y above their centre against the 0.890 assumed here. The four windows centred
on +/-120 appear from their raw min and max to span -172..180, which is the
antimeridian artifact this file's longitude test is written to avoid rather than a
wider reach.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from src.scraper.sources.adsbx_map_source import ADSBX_REGIONS, ADSBxMapTaskSource

# Mercator x is linear in longitude and the measured span was symmetric about the
# window centre, so half of it is the reach either side.
MEASURED_LON_REACH = 182.0 / 2

# Half-reach in Mercator y, taken from the northern half of the measured span
# (y(73) - y(50)). The southern half measured larger; using the smaller of the
# two is the conservative choice, since the larger may be where the loaded region
# ended rather than where the window did.
MEASURED_Y_REACH = 0.890

# Every latitude that carries meaningful traffic. Beyond this are the polar caps,
# which no commercial or military route crosses in numbers worth a window.
TRAFFIC_LAT_MIN = -60.0
TRAFFIC_LAT_MAX = 70.0


def _y(lat: float) -> float:
    """Web Mercator northing for a latitude, the space the grid is spaced in."""
    return math.asinh(math.tan(math.radians(lat)))


def _source(**overrides: Any) -> ADSBxMapTaskSource:
    """An ADSBx task source built from a minimal in-memory config."""
    return ADSBxMapTaskSource(
        {"scraper": {"scrapers": {"adsbx_map": {"global_coverage": True, **overrides}}}},
        database_url="",
    )


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Collapse overlapping or touching intervals into disjoint ones."""
    merged: list[tuple[float, float]] = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


class TestGridShape:
    def test_the_grid_is_far_smaller_than_the_fr24_one(self) -> None:
        """The whole point of a separate grid.

        FR24's 50 windows exist because it caps a request at ~1,500 aircraft;
        reusing them for ADSBx re-scraped the same airspace up to eight times per
        cycle and stretched a full pass to about 80 minutes.
        """
        from src.scraper.sources.fr24_map_source import GLOBAL_REGIONS

        assert len(ADSBX_REGIONS) < len(GLOBAL_REGIONS) / 4

    def test_every_window_uses_the_zoom_the_reach_was_measured_at(self) -> None:
        """The reach constants below are only valid at the zoom they came from."""
        assert {r["zoom"] for r in ADSBX_REGIONS.values()} == {3}

    def test_every_window_has_a_valid_centre(self) -> None:
        for name, region in ADSBX_REGIONS.items():
            assert -90.0 <= region["lat"] <= 90.0, name
            assert -180.0 <= region["lon"] <= 180.0, name

    def test_names_are_usable_as_task_keys(self) -> None:
        """`task_key` is a persisted contract, so no spaces or punctuation."""
        for name in ADSBX_REGIONS:
            assert name.replace("_", "").isalnum(), name


class TestCoverage:
    def test_latitude_is_covered_with_no_seam(self) -> None:
        """A gap between the northern and southern rows loses aircraft silently."""
        bands = _merge(
            [
                (_y(r["lat"]) - MEASURED_Y_REACH, _y(r["lat"]) + MEASURED_Y_REACH)
                for r in ADSBX_REGIONS.values()
            ]
        )

        assert len(bands) == 1, f"latitude coverage is discontinuous: {bands}"
        low, high = bands[0]
        assert low <= _y(TRAFFIC_LAT_MIN)
        assert high >= _y(TRAFFIC_LAT_MAX)

    def test_the_rows_overlap_across_the_equator(self) -> None:
        """Not just contiguous — abutting exactly would make the seam a rounding
        error away from a gap, and the measured reach is an estimate.

        The overlap is asked to be a real band rather than a hairline: a quarter
        of a window's reach in y is roughly 27 degrees here.
        """
        norths = [_y(r["lat"]) for r in ADSBX_REGIONS.values() if r["lat"] > 0]
        souths = [_y(r["lat"]) for r in ADSBX_REGIONS.values() if r["lat"] < 0]
        assert norths and souths

        lowest_north = min(norths) - MEASURED_Y_REACH
        highest_south = max(souths) + MEASURED_Y_REACH
        assert highest_south - lowest_north > MEASURED_Y_REACH / 4, (
            "the two rows barely meet across the equator"
        )

    def test_every_meridian_is_covered(self) -> None:
        """Checked on the doubled line so a window spanning the antimeridian is
        not mistaken for a gap at +/-180."""
        spans: list[tuple[float, float]] = []
        for region in ADSBX_REGIONS.values():
            centre = region["lon"]
            for shift in (-360.0, 0.0, 360.0):
                spans.append(
                    (
                        centre + shift - MEASURED_LON_REACH,
                        centre + shift + MEASURED_LON_REACH,
                    )
                )

        for band in _merge(spans):
            if band[0] <= -180.0 and band[1] >= 180.0:
                return
        pytest.fail(f"longitude coverage has a gap: {_merge(spans)}")

    def test_over_coverage_stays_modest(self) -> None:
        """Overlap is deliberate — a gap loses aircraft, an overlap only costs
        duplicate rows — but it is also what the write volume is paid in.

        Measured in the same y/longitude space the grid is laid out in, against
        the band the grid claims to cover rather than the whole sphere.
        """
        needed = (_y(TRAFFIC_LAT_MAX) - _y(TRAFFIC_LAT_MIN)) * 360.0
        covered = len(ADSBX_REGIONS) * (2 * MEASURED_Y_REACH) * (2 * MEASURED_LON_REACH)

        assert 1.0 < covered / needed < 2.5


class TestRegionSelection:
    def test_global_coverage_uses_the_whole_grid(self) -> None:
        assert set(_source().regions) == set(ADSBX_REGIONS)

    def test_priority_trims_the_grid(self) -> None:
        """Priority 1 is the northern row, where nearly all the traffic is."""
        regions = _source(max_priority=1).regions

        assert regions
        assert set(regions) < set(ADSBX_REGIONS)
        assert all(r["priority"] == 1 for r in regions.values())

    def test_high_priority_regions_come_first(self) -> None:
        priorities = [r.get("priority", 1) for r in _source().regions.values()]

        assert priorities == sorted(priorities)

    def test_enabled_regions_restricts_the_grid(self) -> None:
        regions = _source(enabled_regions=["north_asia"], max_priority=5).regions

        assert list(regions) == ["north_asia"]

    def test_include_oceans_is_accepted_and_changes_nothing(self) -> None:
        """Kept only so an existing config file does not need editing: every ADSBx
        window spans ocean and land, so there is nothing to exclude."""
        assert set(_source(include_oceans=False).regions) == set(ADSBX_REGIONS)

    def test_custom_regions_replace_the_grid(self) -> None:
        source = ADSBxMapTaskSource(
            {
                "scraper": {
                    "scrapers": {
                        "adsbx_map": {
                            "global_coverage": False,
                            "regions": [{"name": "korea", "lat": 37.0, "lon": 127.0, "zoom": 6}],
                        }
                    }
                }
            },
            database_url="",
        )

        assert list(source.regions) == ["korea"]
        assert source.regions["korea"]["zoom"] == 6

    def test_an_empty_custom_list_falls_back_to_the_grid(self) -> None:
        """Otherwise a config with neither key scrapes nothing at all."""
        source = ADSBxMapTaskSource(
            {"scraper": {"scrapers": {"adsbx_map": {"global_coverage": False}}}},
            database_url="",
        )

        assert set(source.regions) == set(ADSBX_REGIONS)


class TestCycleGap:
    def test_min_cycle_gap_is_read_when_interval_seconds_is_absent(self) -> None:
        """Production sets `min_cycle_gap`; the shorter alias is for local runs."""
        assert _source(min_cycle_gap=1800).interval_seconds == 1800

    def test_interval_seconds_wins(self) -> None:
        assert _source(min_cycle_gap=1800, interval_seconds=60).interval_seconds == 60
