
import math

from pyproj import Transformer
from shapely import make_valid, transform
from shapely.geometry import Polygon


TRANSFORMER = Transformer.from_crs(
    "EPSG:4326",
    "EPSG:25832",
    always_xy=True,
)


def calculate_footprint(coordinates):
    """Return a validated, projected polygon and its area in m²."""

    if not isinstance(coordinates, list) or len(coordinates) < 4:
        raise ValueError("Insufficient polygon coordinates.")

    try:
        points = [
            (float(point["lon"]), float(point["lat"]))
            for point in coordinates
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Malformed polygon coordinates.") from exc

    if not all(
        math.isfinite(lon)
        and math.isfinite(lat)
        and -180 <= lon <= 180
        and -90 <= lat <= 90
        for lon, lat in points
    ):
        raise ValueError("Coordinates outside valid longitude/latitude range.")

    if points[0] != points[-1]:
        raise ValueError("Polygon boundary is not closed.")

    polygon = Polygon(points)

    if not polygon.is_valid:
        polygon = make_valid(polygon)

    if polygon.is_empty or polygon.geom_type not in (
        "Polygon",
        "MultiPolygon",
    ):
        raise ValueError("Geometry is empty or not polygonal.")

    projected = transform(
        polygon,
        TRANSFORMER.transform,
        interleaved=False,
    )

    if (
        projected.is_empty
        or not projected.is_valid
        or not math.isfinite(projected.area)
        or projected.area <= 0
    ):
        raise ValueError("Invalid projected polygon or area.")

    return projected, projected.area
