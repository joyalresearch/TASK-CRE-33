
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


SOURCE = Path("data/raw/bauhaus_germany_pois.json")
OUTPUT_DIR = Path("data/processed/store_building_matches")
SCRIPT = Path("src/iris_cre_33/discover_store_buildings.py")

BRAND_WIKIDATA = "Q672043"


def get_nodes():
    data = json.loads(SOURCE.read_text(encoding="utf-8"))

    return sorted(
        (
            item
            for item in data["elements"]
            if item.get("type") == "node"
            and item.get("tags", {}).get("brand:wikidata")
            == BRAND_WIKIDATA
        ),
        key=lambda item: int(item["id"]),
    )


def output_path(node_id):
    return OUTPUT_DIR / f"node_{node_id}.json"


def is_completed(node_id):
    path = output_path(node_id)

    if not path.exists():
        return False

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        return (
            isinstance(data, dict)
            and int(data.get("store_osm_id", -1)) == node_id
            and isinstance(data.get("matches"), list)
        )
    except (ValueError, TypeError, OSError):
        return False


def run_node(node_id, timeout):
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--node-id",
                str(node_id),
            ],
            timeout=timeout,
            check=False,
        )

        return result.returncode == 0 and is_completed(node_id)

    except subprocess.TimeoutExpired:
        print(
            f"TIMEOUT: Node {node_id} exceeded "
            f"{timeout} seconds.",
            flush=True,
        )
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Resume BAUHAUS store-building discovery"
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum pending nodes to process; 0 means all",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=10.0,
        help="Minimum seconds between requests",
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Additional attempts after a failed attempt",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Maximum seconds per node attempt",
    )

    args = parser.parse_args()

    if args.limit < 0:
        parser.error("--limit must be nonnegative")

    if args.delay < 10:
        parser.error("--delay must be at least 10 seconds")

    if not 0 <= args.retries <= 3:
        parser.error("--retries must be between 0 and 3")

    if args.timeout < 30:
        parser.error("--timeout must be at least 30 seconds")

    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)

    if not SCRIPT.exists():
        raise FileNotFoundError(SCRIPT)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    nodes = get_nodes()

    pending = [
        node
        for node in nodes
        if not is_completed(int(node["id"]))
    ]

    selected = (
        pending[:args.limit]
        if args.limit > 0
        else pending
    )

    print("\nBAUHAUS DISCOVERY — RESUMABLE RUN")
    print("--------------------------------")
    print("Total verified nodes:", len(nodes))
    print("Already completed:", len(nodes) - len(pending))
    print("Remaining before run:", len(pending))
    print("Selected for this run:", len(selected))
    print("Delay between attempts:", args.delay, "seconds")
    print("Maximum attempts per node:", args.retries + 1)

    successes = []
    failures = []
    last_attempt_finished = None

    for position, node in enumerate(selected, start=1):
        node_id = int(node["id"])

        print(
            f"\n[{position}/{len(selected)}] "
            f"Node {node_id}",
            flush=True,
        )

        completed = False

        for attempt in range(1, args.retries + 2):
            if last_attempt_finished is not None:
                elapsed = time.monotonic() - last_attempt_finished
                wait = max(0, args.delay - elapsed)

                if wait > 0:
                    print(
                        f"Waiting {wait:.1f} seconds...",
                        flush=True,
                    )
                    time.sleep(wait)

            print(
                f"Attempt {attempt}/{args.retries + 1}",
                flush=True,
            )

            completed = run_node(node_id, args.timeout)
            last_attempt_finished = time.monotonic()

            if completed:
                break

            if attempt <= args.retries:
                backoff = min(120, 20 * (2 ** (attempt - 1)))
                print(
                    f"Retrying after {backoff} seconds...",
                    flush=True,
                )
                time.sleep(backoff)

        if completed:
            successes.append(node_id)
            print("SUCCESS", flush=True)
        else:
            failures.append(node_id)
            print(
                "FAILED — continuing to next node",
                flush=True,
            )

    remaining = [
        int(node["id"])
        for node in nodes
        if not is_completed(int(node["id"]))
    ]

    print("\nDISCOVERY SUMMARY")
    print("--------------------------------")
    print("Successful this run:", len(successes))
    print("Failed this run:", len(failures))
    print("Failed node IDs:", failures)
    print("Total completed:", len(nodes) - len(remaining))
    print("Still pending:", len(remaining))
    print("Pending node IDs:", remaining)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
