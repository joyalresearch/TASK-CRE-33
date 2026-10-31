import argparse
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UNCERTAIN_STATUSES = {"request_pending", "unknown_outcome"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def valid_capture(folder, postcode):
    expected_url = f"https://www.bauhaus.info/api/stores?q={postcode}"

    try:
        manifest = json.loads(
            (folder / "manifest.json").read_text(encoding="utf-8")
        )
        raw = (folder / "source_response.json").read_bytes()

        if not (
            manifest.get("status") == "validated"
            and manifest.get("postcode") == postcode
            and manifest.get("source_url") == expected_url
            and manifest.get("fetch", {}).get("httpStatusCode") == 200
            and hashlib.sha256(raw).hexdigest()
            == manifest.get("source_sha256")
        ):
            return False

        stores = json.loads(raw)
        if not isinstance(stores, list) or not stores:
            return False

        ids = [store["store_id"] for store in stores]
        return (
            all(type(store_id) is int for store_id in ids)
            and len(ids) == len(set(ids))
            and manifest.get("store_count") == len(stores)
        )

    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def cached_capture(postcode, root):
    for folder in sorted(root.glob(f"{postcode}_*")):
        if folder.is_dir() and valid_capture(folder, postcode):
            return folder
    return None


def uncertain_captures(postcode, root):
    """Block fresh requests after a crash or an ambiguous remote outcome."""
    uncertain = []

    for folder in sorted(root.glob(f"{postcode}_*")):
        if not folder.is_dir():
            continue

        try:
            manifest = json.loads(
                (folder / "manifest.json").read_text(encoding="utf-8")
            )
            if manifest.get("status") in UNCERTAIN_STATUSES:
                uncertain.append(str(folder))
        except (OSError, ValueError, AttributeError):
            # A partial attempt may have been interrupted before metadata saved.
            uncertain.append(str(folder))

    return uncertain


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Collect official BAUHAUS locator responses with caching "
            "and bounded retries for confirmed upstream 502 failures."
        )
    )
    parser.add_argument("--all-seeds", action="store_true")
    parser.add_argument(
        "--max-new-runs",
        type=int,
        default=3,
        help="Total new subprocess attempts, including retries.",
    )
    parser.add_argument(
        "--seeds",
        type=Path,
        default=(
            PROJECT_ROOT
            / "data/processed/bauhaus_locator_postcode_seeds.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data/raw/bauhaus_locator",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
    )
    args = parser.parse_args()

    if args.max_new_runs < 0:
        parser.error("--max-new-runs cannot be negative")

    env_file = args.env_file.resolve()
    load_dotenv(env_file, override=False)

    seed_path = args.seeds.resolve()
    seed_bytes = seed_path.read_bytes()
    seed_records = json.loads(seed_bytes)["seeds"]
    values = [seed["postcode"] for seed in seed_records]

    if not values:
        raise ValueError("The seed inventory is empty")

    if any(
        not (
            isinstance(value, str)
            and len(value) == 5
            and value.isascii()
            and value.isdigit()
        )
        for value in values
    ):
        raise ValueError("Seed inventory contains an invalid postcode")

    postcodes = sorted(set(values))

    if args.all_seeds:
        selected = postcodes
    else:
        if len(postcodes) < 3:
            raise ValueError("The pilot requires at least three seeds")

        # Spread across postcode order, not verified geographic coverage.
        selected = [
            postcodes[0],
            postcodes[len(postcodes) // 2],
            postcodes[-1],
        ]

    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)

    fetch_script = PROJECT_ROOT / "src/iris_cre_33/fetch_locator_apify.py"
    if not fetch_script.is_file():
        raise FileNotFoundError(fetch_script)

    cached = {
        postcode: cached_capture(postcode, root)
        for postcode in selected
    }
    needed = sum(folder is None for folder in cached.values())

    print("Selected postcodes:", len(selected), flush=True)
    print("Validated cached captures:", len(selected) - needed, flush=True)
    print("New acquisitions needed:", needed, flush=True)
    print("New-run attempt limit:", args.max_new_runs, flush=True)
    print("Retries: at most 2 per postcode, after 30 and 60 seconds",
          flush=True)

    if needed > args.max_new_runs:
        raise RuntimeError(
            f"{needed} initial acquisitions are needed, but the limit "
            f"is {args.max_new_runs}. No new runs were started."
        )

    report_path = root / f"collection_{uuid.uuid4().hex}.json"
    report = {
        "started_at_utc": utc_now(),
        "mode": "all_seeds" if args.all_seeds else "pilot",
        "seed_file": str(seed_path),
        "seed_sha256": hashlib.sha256(seed_bytes).hexdigest(),
        "output_dir": str(root),
        "selected_postcodes": selected,
        "max_new_runs": args.max_new_runs,
        "new_run_attempts": 0,
        "max_attempts_per_postcode": 3,
        "retry_delays_seconds": [30, 60],
        "coverage_verified": False,
        "status": "running",
        "results": [],
    }

    environment = os.environ.copy()
    token = os.environ.get("APIFY_TOKEN", "").strip() or None

    def save_report():
        save_json(report_path, report)

    try:
        save_report()

        for position, postcode in enumerate(selected, start=1):
            print(
                f"\n[{position}/{len(selected)}] Postcode {postcode}",
                flush=True,
            )

            # Recheck disk, including any success from a previous invocation.
            capture = cached_capture(postcode, root)

            if capture is not None:
                print("Using validated cache:", capture, flush=True)
                report["results"].append({
                    "postcode": postcode,
                    "status": "cached",
                    "capture": str(capture),
                })
                save_report()
                continue

            record = {
                "postcode": postcode,
                "status": "starting",
                "started_at_utc": utc_now(),
                "attempts": [],
            }
            report["results"].append(record)
            save_report()

            uncertain = uncertain_captures(postcode, root)
            if uncertain:
                record["status"] = "unknown_outcome"
                record["uncertain_captures"] = uncertain
                raise RuntimeError(
                    "An earlier attempt has an uncertain outcome. "
                    "Inspect it before starting another remote run."
                )

            if token is None:
                token = getpass("Apify token (hidden): ").strip()
                if not token:
                    raise ValueError("Apify token is empty")

            environment["APIFY_TOKEN"] = token

            for attempt_number in range(1, 4):
                if report["new_run_attempts"] >= args.max_new_runs:
                    record["status"] = "attempt_limit_reached"
                    raise RuntimeError(
                        "Total new-run attempt limit reached. "
                        "Saved captures are preserved."
                    )

                capture_id = uuid.uuid4().hex
                attempt_folder = root / f"{postcode}_{capture_id}"
                attempt = {
                    "attempt": attempt_number,
                    "capture": str(attempt_folder),
                    "started_at_utc": utc_now(),
                    "status": "starting",
                }
                record["attempts"].append(attempt)
                report["new_run_attempts"] += 1
                save_report()

                print(
                    f"Acquiring: {postcode}, attempt {attempt_number}/3",
                    flush=True,
                )

                result = subprocess.run(
                    [
                        sys.executable,
                        str(fetch_script),
                        "--postcode",
                        postcode,
                        "--live",
                        "--output-dir",
                        str(root),
                        "--env-file",
                        str(env_file),
                        "--capture-id",
                        capture_id,
                    ],
                    env=environment,
                    cwd=PROJECT_ROOT,
                    check=False,
                )

                attempt["returncode"] = result.returncode
                attempt["finished_at_utc"] = utc_now()

                try:
                    manifest = json.loads(
                        (attempt_folder / "manifest.json").read_text(
                            encoding="utf-8"
                        )
                    )
                except (OSError, ValueError):
                    attempt["status"] = "unknown_outcome"
                    record["status"] = "unknown_outcome"
                    raise RuntimeError(
                        "Attempt metadata is unavailable; "
                        "inspect before restarting."
                    )

                attempt["run_id"] = manifest.get("run_id")

                if (
                    result.returncode == 0
                    and valid_capture(attempt_folder, postcode)
                ):
                    attempt["status"] = "validated"
                    record.update({
                        "status": "acquired",
                        "capture": str(attempt_folder),
                        "finished_at_utc": utc_now(),
                    })
                    save_report()
                    break

                retryable = (
                    manifest.get("retryable") is True
                    and manifest.get("run_status") == "FAILED"
                    and manifest.get("retry_reason")
                    == "confirmed_failed_upstream_502"
                )

                attempt["status"] = (
                    "confirmed_upstream_failure"
                    if retryable else "failed_or_unknown"
                )
                save_report()

                if not retryable:
                    record["status"] = "failed_or_unknown"
                    raise RuntimeError(
                        "Failure is not a confirmed upstream 502. "
                        "Inspect the saved evidence before restarting."
                    )

                if attempt_number == 3:
                    record.update({
                        "status": "unresolved_after_retries",
                        "finished_at_utc": utc_now(),
                    })
                    print(
                        f"Unresolved: {postcode}; continuing to next seed.",
                        flush=True,
                    )
                    save_report()
                    break

                delay = 30 * attempt_number
                attempt["retry_delay_seconds"] = delay
                save_report()
                print(f"Retrying in {delay} seconds.", flush=True)
                time.sleep(delay)

            if position < len(selected):
                time.sleep(3)

        report["status"] = "completed"
        print("\nSelected-seed collection finished.", flush=True)
        print("Nationwide coverage remains unverified.", flush=True)

    except BaseException as error:
        report["status"] = "interrupted_or_failed"
        report["error_type"] = type(error).__name__

        if report["results"]:
            last = report["results"][-1]
            if last["status"] == "starting":
                last["status"] = "failed_or_unknown"

            if last.get("attempts"):
                attempt = last["attempts"][-1]
                if attempt["status"] == "starting":
                    attempt["status"] = "unknown_outcome"

        raise

    finally:
        environment.pop("APIFY_TOKEN", None)

        report["unresolved_postcodes"] = [
            item["postcode"]
            for item in report["results"]
            if item["status"] not in {"cached", "acquired"}
        ]
        processed = {item["postcode"] for item in report["results"]}
        report["unprocessed_postcodes"] = [
            postcode for postcode in selected
            if postcode not in processed
        ]

        if (
            report["status"] == "completed"
            and report["unresolved_postcodes"]
        ):
            report["status"] = "completed_with_unresolved"

        report["finished_at_utc"] = utc_now()
        save_report()
        print(
            "\nUnresolved postcodes:",
            len(report["unresolved_postcodes"]),
            flush=True,
        )
        print("Collection report:", report_path.resolve(), flush=True)


if __name__ == "__main__":
    main()