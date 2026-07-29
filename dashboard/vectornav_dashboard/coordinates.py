"""Local track coordinate conversion."""

from __future__ import annotations

import math


EARTH_RADIUS_METERS = 6_371_000.0


def local_xy(
    latitude: float,
    longitude: float,
    origin_latitude: float,
    origin_longitude: float,
) -> tuple[float, float]:
    north = EARTH_RADIUS_METERS * math.radians(latitude - origin_latitude)
    east = (
        EARTH_RADIUS_METERS
        * math.cos(math.radians(origin_latitude))
        * math.radians(longitude - origin_longitude)
    )
    return east, north
