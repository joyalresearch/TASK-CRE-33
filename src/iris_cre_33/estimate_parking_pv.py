import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path
import uuid

from shapely import from_wkt
from shapely.ops import unary_union

from load_parking import read_cologne, read_nationwide
from reconciled_inputs import build_records


PROJECT_ROOT = Path(__file__).resolve().parents[2]

REFERENCE_URL = (
    "https://www.ise.fraunhofer.de/content/dam/ise/de/"
    "documents/presseinformationen/2024/Abschlussbericht_Stromstudie_Ba-Wue_V05_080224.pdf"
)

RULE_VERSION = "bauhaus-explicit-tags-v1"


def words(value):
    text = unicodedata.normalize("NFKD", str(value).casefold())
    text = "".join(
        char for char in text
        if not unicodedata.combining(char)
    )
    return set(re.findall(r"[a-z0-9]+", text))


def classify_parking_identity(tags, official_names=()):
    """Require explicit BAUHAUS text evidence; withhold ambiguity."""
    evidence = {
        key: str(tags[key]).strip()
        for key in ("brand", "operator", "name")
        if tags.get(key) is not None and str(tags[key]).strip()
    }

    legal = {
        "bauhaus", "gmbh", "co", "kg", "ag", "fa", "firma",
    }
    generic = {
        "parkplatz", "parkplatze", "parking", "car", "park",
        "kundenparkplatz", "kundenparkplatze", "kunden",
        "customer", "customers", "besucherparkplatz",
        "p", "der", "des", "am",
    }

    location_words = set()
    for name in official_names:
        location_words.update(words(name))

    name_allowed = legal | generic | location_words
    reasons = []
    explicit = False

    for key in ("brand", "operator"):
        value = evidence.get(key)
        if value is None:
            continue

        tokens = words(value)

        if "bauhaus" not in tokens:
            reasons.append(
                f"{key}_does_not_identify_bauhaus"
            )
        elif not tokens <= legal:
            reasons.append(
                f"{key}_contains_unreviewed_identity_text"
            )
        else:
            explicit = True

    name = evidence.get("name")
    if name is not None:
        tokens = words(name)

        # Delimiters can indicate jointly named parking.
        if any(
            marker in name
            for marker in ("/", ";", "+", "&")
        ):
            reasons.append(
                "name_has_shared_or_ambiguous_identity"
            )
        elif tokens - name_allowed:
            reasons.append(
                "name_contains_other_or_unreviewed_identity_text"
            )
        elif "bauhaus" in tokens:
            explicit = True
        elif not tokens <= generic:
            reasons.append(
                "name_does_not_identify_bauhaus"
            )

    for key in ("brand:wikidata", "operator:wikidata"):
        if (
            tags.get(key)
            and str(tags[key]).strip() != "Q672043"
        ):
            reasons.append(
                f"{key}_conflicts_or_requires_review"
            )

    if reasons:
        status = "conflicting_or_ambiguous_identity_review"
    elif explicit:
        status = "explicit_bauhaus_tag_candidate"
        reasons = [
            "explicit_bauhaus_text_without_detected_identity_conflict"
        ]
    else:
        status = "no_explicit_bauhaus_identity"
        reasons = [
            "proximity_or_generic_parking_tags_are_insufficient"
        ]

    return {
        "status": status,
        "reasons": reasons,
        "evidence": evidence,
    }


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive(value, label):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{label} must be positive and finite"
        )
    return value


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Estimate provisional parking-canopy PV scenarios "
            "for explicitly BAUHAUS-tagged parking."
        )
    )
    parser.add_argument(
        "--pv-report", type=Path, required=True
    )
    parser.add_argument(
        "--buildings",
        type=Path,
        default=PROJECT_ROOT / (
            "data/processed/bauhaus_buildings_consolidated.json"
        ),
    )
    parser.add_argument(
        "--cologne",
        type=Path,
        default=PROJECT_ROOT / (
            "data/raw/parking_cologne_raw.json"
        ),
    )
    parser.add_argument(
        "--nationwide",
        type=Path,
        default=PROJECT_ROOT / (
            "data/processed/parking_nationwide_candidates.json"
        ),
    )
    parser.add_argument(
        "--radius-m", type=float, default=150.0
    )
    parser.add_argument(
        "--canopy-share", type=float, default=0.50
    )
    parser.add_argument(
        "--sqm-per-kwp", type=float, default=5.5
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data/processed",
    )
    args = parser.parse_args()

    positive(args.radius_m, "Candidate search radius")
    positive(args.sqm_per_kwp, "Square metres per kWp")
    positive(args.canopy_share, "Canopy share")

    if args.canopy_share > 1:
        raise ValueError(
            "Canopy share cannot exceed one"
        )

    buildings = build_records(
        args.buildings.resolve(),
        args.pv_report.resolve(),
    )
    building_geometries = [
        from_wkt(row["geometry_wkt_25832"])
        for row in buildings
    ]
    building_mask = unary_union(building_geometries)

    selected_stores = []

    for row, geometry in zip(
        buildings, building_geometries
    ):
        if row["association_status"] not in {
            "accepted_screening_association",
            "provisional_association",
        }:
            continue

        metadata = row["tags"]["_iris_reconciliation"]

        selected_stores.append({
            "official_store_id": metadata["official_store_id"],
            "association_status": row["association_status"],
            "official_name": metadata["official_name"],
            "geometry": geometry,
        })

    cologne, deferred, rejected = read_cologne(
        args.cologne
    )
    if rejected:
        raise ValueError(
            f"Cologne input has {len(rejected)} "
            "rejected geometries"
        )

    nationwide, old_pair_count = read_nationwide(
        args.nationwide
    )

    # Match the loader's precedence for duplicate identities.
    combined = dict(cologne)
    combined.update(nationwide)

    if not combined:
        raise ValueError(
            "Parking inventory is empty"
        )

    links_by_identity = {}
    identity_reviews = {}

    for identity, feature in combined.items():
        links = []
        names = []

        for store in selected_stores:
            distance = feature["geometry"].distance(
                store["geometry"]
            )

            if distance <= args.radius_m:
                names.append(store["official_name"])

                links.append({
                    "official_store_id": (
                        store["official_store_id"]
                    ),
                    "building_association_status": (
                        store["association_status"]
                    ),
                    "distance_m": round(distance, 2),
                    "parking_association_verified": False,
                })

        links_by_identity[identity] = links
        identity_reviews[identity] = (
            classify_parking_identity(
                feature["tags"], names
            )
        )

    # Remove conflicting/ambiguous parking overlaps from
    # otherwise eligible surfaces.
    identity_exclusion_mask = unary_union([
        combined[identity]["geometry"]
        for identity, review in identity_reviews.items()
        if review["status"] == (
            "conflicting_or_ambiguous_identity_review"
        )
    ])

    raw_shapes = []
    linked_shapes = []
    rows = []

    for identity, feature in sorted(combined.items()):
        geometry = feature["geometry"]
        tags = feature["tags"]

        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type not in {
                "Polygon", "MultiPolygon"
            }
            or not math.isfinite(geometry.area)
            or geometry.area <= 0
        ):
            raise ValueError(
                f"Invalid parking geometry: {identity}"
            )

        raw_shapes.append(geometry)

        links = links_by_identity[identity]
        identity_review = identity_reviews[identity]

        covered = str(
            tags.get("covered", "")
        ).casefold() in {
            "yes", "true", "1",
        }

        solar_tag = any(
            str(tags.get(key, "")).casefold() == "solar"
            for key in (
                "generator:source",
                "plant:source",
            )
        )

        outside_buildings = geometry.difference(
            building_mask
        )
        remaining = outside_buildings.difference(
            identity_exclusion_mask
        )

        if (
            not remaining.is_empty
            and not remaining.is_valid
        ):
            raise ValueError(
                "Invalid geometry after exclusions: "
                f"{identity}"
            )

        if identity_review["status"] != (
            "explicit_bauhaus_tag_candidate"
        ):
            category = identity_review["status"]
        elif covered or solar_tag:
            category = "covered_or_solar_tag_review"
        elif not links:
            category = "no_selected_store_candidate"
        elif len(links) > 1:
            category = "shared_store_candidate"
        elif links[0]["building_association_status"] == (
            "accepted_screening_association"
        ):
            category = "near_accepted_building_candidate"
        else:
            category = "near_provisional_building_candidate"

        included = bool(
            len(links) == 1
            and identity_review["status"] == (
                "explicit_bauhaus_tag_candidate"
            )
            and not covered
            and not solar_tag
            and not remaining.is_empty
            and remaining.area > 0
        )

        if included:
            linked_shapes.append(remaining)

        rows.append({
            "osm_type": identity[0],
            "osm_id": identity[1],
            "source_association_status": (
                feature["association_status"]
            ),
            "review_category": category,
            "identity_review": identity_review,
            "parking_area_sqm": round(
                geometry.area, 2
            ),
            "area_outside_known_buildings_sqm": round(
                outside_buildings.area, 2
            ),
            "area_after_identity_exclusions_sqm": round(
                remaining.area, 2
            ),
            "included_in_conditional_scenario": included,
            "candidate_store_links": links,
            "ownership_verified": False,
            "parking_association_verified": False,
            "installation_feasibility_verified": False,
            "tags": tags,
        })

    all_union = unary_union(raw_shapes)
    linked_union = unary_union(linked_shapes)

    raw_sum = sum(
        shape.area for shape in raw_shapes
    )
    linked_sum = sum(
        shape.area for shape in linked_shapes
    )
    linked_area = linked_union.area

    scenarios = []

    for name, bay_share in (
        ("lower_bay_share", 0.30),
        ("central_bay_share", 0.50),
        ("higher_bay_share", 0.60),
    ):
        module_area = (
            linked_area
            * bay_share
            * args.canopy_share
        )
        capacity = (
            module_area / args.sqm_per_kwp
        )

        scenarios.append({
            "name": name,
            "assumed_parking_bay_fraction": bay_share,
            "assumed_canopied_fraction_of_bays": (
                args.canopy_share
            ),
            "effective_module_area_fraction": (
                bay_share * args.canopy_share
            ),
            "assumed_module_covered_area_sqm": round(
                module_area, 2
            ),
            "conditional_capacity_kwp": round(
                capacity, 2
            ),
            "conditional_capacity_mwp": round(
                capacity / 1000, 3
            ),
            "status": "provisional_sensitivity_scenario",
        })

    counts = Counter(
        row["review_category"] for row in rows
    )

    result = {
        "created_at_utc": (
            datetime.now(timezone.utc).isoformat()
        ),
        "input_files": [
            {
                "path": str(path.resolve()),
                "sha256": file_hash(path),
            }
            for path in (
                args.buildings,
                args.pv_report,
                args.cologne,
                args.nationwide,
            )
        ],
        "area_crs": "EPSG:25832",
        "identity_rule_version": RULE_VERSION,
        "scenario_scope": (
            "Explicit BAUHAUS-tagged, "
            "singly linked parking only"
        ),
        "identity_exclusion_union_sqm": round(
            identity_exclusion_mask.area, 2
        ),
        "parking_polygon_count": len(rows),
        "parking_area_sum_sqm": round(raw_sum, 2),
        "parking_area_union_sqm": round(
            all_union.area, 2
        ),
        "overlapping_parking_area_sqm": round(
            max(0.0, raw_sum - all_union.area), 2
        ),
        "scenario_polygon_count": len(linked_shapes),
        "scenario_surface_union_sqm": round(
            linked_area, 2
        ),
        "scenario_overlap_removed_sqm": round(
            max(0.0, linked_sum - linked_area), 2
        ),
        "review_category_counts": dict(counts),
        "candidate_radius_m": args.radius_m,
        "sqm_per_kwp": args.sqm_per_kwp,
        "reference": {
            "url": REFERENCE_URL,
            "section": "3.5, pages 36-37",
            "scope": (
                "Baden-Württemberg study; "
                "adapted scenario assumptions"
            ),
            "central_bay_share": 0.50,
            "central_canopy_share_of_bays": 0.50,
            "bay_share_sensitivity": [
                0.30, 0.50, 0.60
            ],
            "density_basis": (
                "Approximately 5.5 m2/kWp derived from Fraunhofer "
                "ISE Stromstudie Baden-Wuerttemberg, January 2024, "
                "section 3.5, printed page 37, Table 8"
                if args.sqm_per_kwp == 5.5
                else "User-supplied scenario density"
            ),
            "density_derivation": {
                "source_module_area_sqm": 14392257,
                "source_capacity_mwp": 2617,
                "derived_sqm_per_kwp": 14392257 / (2617 * 1000),
                "rounded_reference_sqm_per_kwp": 5.5,
                "source_scope": "Table 8 subtotal for parking with at least 35 bays",
                "site_specific_validation": False,
            },
        },
        "scenarios": scenarios,
        "records": rows,
        "deferred_cologne_relations": deferred,
        "previous_building_parking_pair_count": (
            old_pair_count
        ),
        "verified_parking_ownership_count": 0,
        "accepted_parking_capacity_kwp": None,
        "combined_rooftop_and_parking_total": None,
        "limitations": [
            "All parking capacity scenarios are provisional.",
            (
                "Only explicit BAUHAUS text tags without "
                "detected conflicts qualify."
            ),
            (
                "Other/unreviewed operators, ambiguous names "
                "and shared links are withheld."
            ),
            (
                "Identity screening uses conservative text "
                "rules, not verified ownership."
            ),
            (
                "Conflict/ambiguity polygon overlaps are "
                "removed from scenario surfaces."
            ),
            (
                "Proximity does not establish parking "
                "ownership or store allocation."
            ),
            (
                "Only parking near currently selected "
                "building candidates is modelled."
            ),
            (
                "Unresolved store geometry may omit "
                "relevant parking candidates."
            ),
            (
                "Parking linked to multiple selected "
                "stores is withheld."
            ),
            (
                "Known building footprints are subtracted "
                "before modelling."
            ),
            (
                "Known covered/solar-tagged surfaces "
                "are withheld for review."
            ),
            "Existing solar installations may be untagged.",
            (
                "Bay fractions are borrowed scenario "
                "assumptions, not site measurements."
            ),
            (
                "No layout, clearance, tree/shading, "
                "structural or grid assessment."
            ),
            (
                "No permit, commercial-rights or "
                "economic feasibility assessment."
            ),
            (
                "The scenarios are not confidence "
                "intervals or nationwide totals."
            ),
        ],
    }

    args.output_dir.mkdir(
        parents=True, exist_ok=True
    )
    output = (
        args.output_dir
        / f"parking_pv_scenarios_{uuid.uuid4().hex}.json"
    )

    with output.open("x", encoding="utf-8") as handle:
        json.dump(
            result,
            handle,
            ensure_ascii=False,
            indent=2,
        )

    print("Identity rule:", RULE_VERSION)
    print(
        "Unique surface-parking polygons:",
        len(rows),
    )
    print(
        "All-source audit union sqm (not BAUHAUS area):",
        round(all_union.area, 2),
    )
    print(
        "Scenario parking polygons:",
        len(linked_shapes),
    )
    print(
        "Scenario surface union area sqm:",
        round(linked_area, 2),
    )
    print(
        "Parking overlap removed from scenario sqm:",
        round(max(0.0, linked_sum - linked_area), 2),
    )

    for category, count in sorted(counts.items()):
        print(f"{category}: {count}")

    for scenario in scenarios:
        print(
            scenario["name"],
            "| assumed module area sqm:",
            scenario["assumed_module_covered_area_sqm"],
            "| provisional MWp:",
            scenario["conditional_capacity_mwp"],
        )

    print("Verified parking capacity: unavailable")
    print("Database writes: none")
    print("Saved:", output.resolve())


if __name__ == "__main__":
    main()