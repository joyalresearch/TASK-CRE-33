"""Build staging records from geometry and reconciled PV evidence.

Only accepted screening estimates populate the staging capacity column.
Provisional scenarios remain separate in record metadata.
"""

import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath

from shapely import from_wkt


PROJECT_ROOT = Path(__file__).resolve().parents[2]
META_KEY = "_iris_reconciliation"


def read_checked(path, expected=None):
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    if expected is not None and digest != expected:
        raise ValueError(f"Input hash mismatch: {Path(path).name}")

    return json.loads(raw), digest


def resolve_reference(reference, root=PROJECT_ROOT):
    """Resolve saved evidence paths after moving the project."""
    direct = Path(reference)
    if direct.is_file():
        return direct

    parts = PurePosixPath(
        str(reference).replace("\\", "/")
    ).parts

    if ".." in parts:
        raise ValueError("Parent traversal in recorded input reference")

    if "data" in parts:
        candidate = root.joinpath(*parts[parts.index("data"):])
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        f"Referenced evidence missing: {reference}"
    )


def finite_positive(value, label):
    if isinstance(value, bool):
        raise ValueError(f"Invalid {label}")

    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Invalid {label}")

    return value


def build_records(building_path, pv_path, root=PROJECT_ROOT):
    report, report_hash = read_checked(pv_path)
    inputs = report["inputs"]

    buildings, building_hash = read_checked(
        building_path,
        inputs["buildings"]["sha256"],
    )

    decision_path = resolve_reference(
        inputs["decisions"]["path"], root
    )
    decisions, decision_hash = read_checked(
        decision_path,
        inputs["decisions"]["sha256"],
    )

    gap_path = resolve_reference(
        inputs["cached_gap_search"]["path"], root
    )
    gaps, gap_hash = read_checked(
        gap_path,
        inputs["cached_gap_search"]["sha256"],
    )

    if decisions.get("cached_gap_search_sha256") != gap_hash:
        raise ValueError("Decision/gap evidence hash mismatch")

    assumptions = report["assumptions"]
    if assumptions.get("area_crs") != "EPSG:25832":
        raise ValueError("Unexpected PV geometry CRS")

    fraction = finite_positive(
        assumptions["usable_fraction"], "usable fraction"
    )
    density = finite_positive(
        assumptions["sqm_per_kwp"], "sqm per kWp"
    )

    if fraction > 1:
        raise ValueError("Usable fraction exceeds one")

    features = {}

    def add(feature):
        feature = copy.deepcopy(feature)

        if feature.get("source") != "openstreetmap":
            raise ValueError("Unexpected geometry source")

        kind = feature["osm_type"]
        osm_id = feature["osm_id"]

        if (
            kind not in {"way", "relation"}
            or type(osm_id) is not int
            or osm_id <= 0
        ):
            raise ValueError("Invalid OSM identity")

        key = f"{kind}/{osm_id}"

        if feature["feature_type"] != "building":
            raise ValueError("Unexpected feature type")

        geometry = from_wkt(feature["geometry_wkt_25832"])

        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type not in {"Polygon", "MultiPolygon"}
            or not math.isfinite(geometry.area)
            or geometry.area <= 0
        ):
            raise ValueError(f"Invalid geometry: {key}")

        area = finite_positive(
            feature["area_sqm"], "recorded area"
        )
        if abs(geometry.area - area) > max(
            0.02, geometry.area * 1e-6
        ):
            raise ValueError(
                f"Recorded geometry area differs: {key}"
            )

        if key in features:
            existing = from_wkt(
                features[key]["geometry_wkt_25832"]
            )
            if not existing.equals(geometry):
                raise ValueError(
                    f"Conflicting geometry: {key}"
                )
            return

        feature["area_sqm"] = round(geometry.area, 2)

        if (
            not isinstance(feature["tags"], dict)
            or META_KEY in feature["tags"]
        ):
            raise ValueError(
                "Unexpected or previously enriched OSM tags"
            )

        feature["association_status"] = "unmatched_osm_candidate"
        feature["estimated_pv_kwp"] = None
        feature["tags"][META_KEY] = {
            "pv_report_sha256": report_hash,
            "decision_sha256": decision_hash,
            "building_source_sha256": building_hash,
            "estimate_bucket": "unavailable",
            "official_store_id": None,
        }
        features[key] = feature

    for feature in buildings["features"]:
        add(feature)

    for candidate in gaps["candidates"]:
        kind, identifier = candidate["osm_id"].split("/")
        add({
            "source": "openstreetmap",
            "osm_type": kind,
            "osm_id": int(identifier),
            "feature_type": "building",
            "geometry_wkt_25832": candidate["geometry_wkt_25832"],
            "area_sqm": candidate["area_sqm"],
            "tags": candidate["tags"],
        })

    def index_unique(rows, key):
        result = {}
        for row in rows:
            identifier = row[key]
            if (
                type(identifier) is not int
                or identifier in result
            ):
                raise ValueError(
                    "Invalid or duplicate official identity"
                )
            result[identifier] = row
        return result

    decision_map = index_unique(
        decisions["decisions"], "official_store_id"
    )
    estimates = index_unique(
        report["records"], "official_store_id"
    )

    if set(decision_map) != set(estimates):
        raise ValueError(
            "PV report dropped or added official stores"
        )

    allowed_statuses = {
        "accepted_screening_association",
        "provisional_association",
        "parking_structure_review",
        "unresolved_no_containment",
    }
    selected = set()

    for identifier, decision in decision_map.items():
        estimate = estimates[identifier]
        status = decision["status"]

        if (
            status not in allowed_statuses
            or estimate["association_status"] != status
        ):
            raise ValueError(
                "Unknown or inconsistent association status"
            )

        key = decision.get("osm_id")
        if estimate.get("osm_id") != key:
            raise ValueError(
                "Selected geometry differs between decision and PV"
            )

        if status == "accepted_screening_association":
            bucket = "accepted"
        elif status == "provisional_association":
            bucket = "provisional"
        else:
            bucket = "unavailable"

        if estimate["estimate_bucket"] != bucket:
            raise ValueError(
                "PV bucket contradicts association status"
            )

        if (
            bucket == "unavailable"
            and estimate.get("estimated_pv_kwp") is not None
        ):
            raise ValueError(
                "Unknown capacity must remain null"
            )

        if key is None:
            if status != "unresolved_no_containment":
                raise ValueError(
                    "Selected building identity missing"
                )
            continue

        if key not in features or key in selected:
            raise ValueError(
                "Missing or multiply allocated polygon"
            )
        selected.add(key)

        feature = features[key]
        geometry = from_wkt(feature["geometry_wkt_25832"])
        computed = round(
            geometry.area * fraction / density, 2
        )

        if bucket != "unavailable":
            actual = finite_positive(
                estimate["estimated_pv_kwp"], "capacity"
            )
            if abs(actual - computed) > 0.011:
                raise ValueError(
                    "PV estimate differs from geometry and assumptions"
                )

            footprint = finite_positive(
                estimate["footprint_area_sqm"],
                "PV footprint area",
            )
            if abs(footprint - geometry.area) > 0.02:
                raise ValueError(
                    "PV footprint area differs from selected geometry"
                )

        if (
            bucket == "accepted"
            and decision.get(
                "association_accepted_for_screening"
            ) is not True
        ):
            raise ValueError(
                "Accepted estimate lacks accepted screening decision"
            )

        feature["association_status"] = status
        feature["estimated_pv_kwp"] = (
            computed if bucket == "accepted" else None
        )

        feature["tags"][META_KEY].update({
            "official_store_id": identifier,
            "estimate_bucket": bucket,
            "official_name": estimate["official_name"],
            "decision_rule_version": decisions["decision_rule_version"],
            "usable_fraction": fraction,
            "sqm_per_kwp": density,
            "provisional_pv_kwp": (
                computed if bucket == "provisional" else None
            ),
            "association_reasons": decision.get("reasons", []),
            "ownership_verified": False,
            "exclusive_roof_use_verified": False,
        })

    return list(features.values())