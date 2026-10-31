import json
from collections import Counter
from pathlib import Path

SOURCE = Path("data/raw/bauhaus_germany_pois.json")
MATCH_DIR = Path("data/processed/store_building_matches")
REPORT_DIR = Path("data/reports")


def main():
    source = json.loads(SOURCE.read_text(encoding="utf-8"))

    nodes = [
        item
        for item in source["elements"]
        if item["type"] == "node"
        and item.get("tags", {}).get("brand:wikidata") == "Q672043"
    ]

    processed = []
    pending = []
    candidates = []
    manual_review = []
    unresolved = []

    for node in nodes:
        node_id = node["id"]
        path = MATCH_DIR / f"node_{node_id}.json"

        if not path.exists():
            pending.append(node_id)
            continue

        result = json.loads(path.read_text(encoding="utf-8"))
        processed.append(node_id)

        matches = result.get("matches", [])

        if not matches:
            unresolved.append(node_id)

        for match in matches:
            record = {
                "store_node_id": node_id,
                "building_way_id": match["osm_way_id"],
                "building_type": match["building_type"],
                "area_sqm": match["area_sqm"],
                "review_status": match["review_status"],
                "review_reason": match["review_reason"],
            }

            if match["review_status"] == "candidate":
                candidates.append(record)
            else:
                manual_review.append(record)

    report = {
        "total_brand_verified_nodes": len(nodes),
        "processed_nodes": len(processed),
        "pending_nodes": len(pending),
        "pending_node_ids": pending,
        "unresolved_nodes": len(unresolved),
        "unresolved_node_ids": unresolved,
        "candidate_matches": len(candidates),
        "manual_review_matches": len(manual_review),
        "candidate_area_sqm": round(
            sum(item["area_sqm"] for item in candidates), 2
        ),
        "manual_review_area_sqm": round(
            sum(item["area_sqm"] for item in manual_review), 2
        ),
        "manual_review_reasons": dict(
            Counter(item["review_reason"] for item in manual_review)
        ),
        "candidates": candidates,
        "manual_review": manual_review,
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    output = REPORT_DIR / "store_discovery_report.json"

    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\nBAUHAUS STORE DISCOVERY REPORT")
    print("--------------------------------")
    print("Verified nodes:", report["total_brand_verified_nodes"])
    print("Processed nodes:", report["processed_nodes"])
    print("Pending nodes:", report["pending_nodes"])
    print("Unresolved nodes:", report["unresolved_nodes"])
    print("Candidate matches:", report["candidate_matches"])
    print("Manual-review matches:", report["manual_review_matches"])
    print("Candidate area (sqm):", report["candidate_area_sqm"])
    print("Manual-review area (sqm):", report["manual_review_area_sqm"])
    print("Report saved:", output)


if __name__ == "__main__":
    main()