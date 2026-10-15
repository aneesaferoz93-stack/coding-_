"""
geo.py — spatial helpers with an explicit PostGIS / shapely fallback.

Silent fallback is a security bug: if the geofence quietly stops working on a
district server without PostGIS, Rule 6 goes blind and nobody notices. Every
function here returns the engine it used so the API can surface it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Mouza


@dataclass(slots=True)
class GeoResult:
    inside: bool | None = None
    distance_m: float | None = None
    engine: str = "none"          # "postgis" | "shapely" | "none"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"inside": self.inside, "distance_m": self.distance_m,
                "engine": self.engine, "reason": self.reason}


def point_wkt(lon: float, lat: float) -> str:
    return f"POINT({lon:.7f} {lat:.7f})"


async def postgis_available(session: AsyncSession) -> bool:
    try:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            return False
        row = (await session.execute(text(
            "SELECT 1 FROM pg_extension WHERE extname='postgis'"
        ))).first()
        return row is not None
    except Exception:
        return False


async def locate_in_mouza(
    session: AsyncSession,
    mouza_code: str,
    lat: float,
    lon: float,
    *,
    wkt: str | None = None,
) -> GeoResult:
    """Is (lat, lon) inside this mouza, and how far is it from the boundary?

    Tries PostGIS first (indexed, exact, handles real cadastral polygons), then
    falls back to shapely on the stored WKT. If the mouza is undigitised we
    return ``inside=None`` — which the policy layer must treat as *unknown*,
    never as *inside*.
    """
    if wkt is None:
        mouza = await session.get(Mouza, mouza_code)
        wkt = mouza.boundary_wkt if mouza else None

    if not wkt:
        return GeoResult(engine="none",
                         reason="mouza boundary not digitised — geofence unavailable")

    if await postgis_available(session):
        try:
            row = (await session.execute(
                text("""
                    SELECT ST_Contains(ST_GeomFromText(:wkt, 4326),
                                       ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)),
                           ST_Distance(ST_GeomFromText(:wkt, 4326)::geography,
                                       ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography)
                """),
                {"wkt": wkt, "lon": lon, "lat": lat},
            )).first()
            if row:
                return GeoResult(inside=bool(row[0]),
                                 distance_m=float(row[1]) if row[1] is not None else None,
                                 engine="postgis")
        except Exception as exc:  # pragma: no cover - surface, don't hide
            return GeoResult(engine="none", reason=f"postgis query failed: {exc}")

    return _shapely_locate(wkt, lat, lon)


def _shapely_locate(wkt: str, lat: float, lon: float) -> GeoResult:
    try:
        from shapely import wkt as shapely_wkt
        from shapely.geometry import Point
    except ImportError:  # pragma: no cover
        return GeoResult(engine="none", reason="shapely not installed")

    try:
        poly = shapely_wkt.loads(wkt)
        pt = Point(lon, lat)
    except Exception as exc:
        return GeoResult(engine="none", reason=f"invalid boundary WKT: {exc}")

    inside = bool(poly.contains(pt))
    # Degrees -> metres, good enough for a 250 m tolerance at KP latitudes.
    deg_dist = poly.exterior.distance(pt) if not inside else 0.0
    metres = deg_dist * 111_320.0
    return GeoResult(inside=inside, distance_m=round(metres, 1), engine="shapely")


def make_box_wkt(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> str:
    """Simple rectangle polygon — used for seeded pilot boundaries."""
    return (
        "POLYGON(("
        f"{min_lon} {min_lat}, {max_lon} {min_lat}, "
        f"{max_lon} {max_lat}, {min_lon} {max_lat}, "
        f"{min_lon} {min_lat}))"
    )
