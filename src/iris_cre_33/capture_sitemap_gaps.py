import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from dotenv import load_dotenv

from fetch_locator_apify import classify_failure


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def cached_page(root, source_url):
    """Reuse a page only when its saved content passes integrity checks."""
    for path in sorted(root.glob("page_*/manifest.json")):
        try:
            manifest = json.loads(path.read_bytes())
            if (
                manifest.get("status") == "captured"
                and manifest.get("source_url") == source_url
                and sha256(
                    (path.parent / "source_response.txt").read_bytes()
                ) == manifest.get("source_sha256")
                and sha256(
                    (path.parent / "dataset_response.bin").read_bytes()
                ) == manifest.get("dataset_sha256")
            ):
                return path.parent

            if (
                manifest.get("source_url") == source_url
                and manifest.get("status")
                == "confirmed_terminal_unresolved"
            ):
                return path.parent

            if (
                manifest.get("source_url") == source_url
                and manifest.get("status")
                in {"request_pending", "unknown_outcome"}
            ):
                raise RuntimeError(
                    f"Uncertain earlier request; inspect {path.parent}"
                )
        except (OSError, ValueError, KeyError, AttributeError):
            continue

    return None


def capture_page(source_url, token, root):
    folder = root / f"page_{uuid.uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / "manifest.json"
    actor_input = {"url": source_url, "formats": ["raw"]}

    manifest = {
        "started_at_utc": utc_now(),
        "source_url": source_url,
        "actor": "apify/web-fetch",
        "actor_input": actor_input,
        "status": "prepared",
        "retryable": False,
        "coverage_verified": False,
        "python_post_retries": 0,
        "provider_internal_retries": "provider-controlled",
    }
    save_json(path, manifest)

    try:
        parameters = urllib.parse.urlencode({
            "timeout": 120,
            "maxTotalChargeUsd": "0.01",
            "format": "json",
            "clean": "false",
        })
        api_url = (
            "https://api.apify.com/v2/actors/"
            "apify~web-fetch/run-sync-get-dataset-items?"
            + parameters
        )
        manifest["api_url"] = api_url

        request = urllib.request.Request(
            api_url,
            data=json.dumps(actor_input).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        manifest["status"] = "request_pending"
        save_json(path, manifest)

        try:
            response = urllib.request.urlopen(request, timeout=150)
        except urllib.error.HTTPError as error:
            response = error

        with response:
            body = response.read()
            manifest["apify_http_status"] = response.status
            manifest["run_id"] = response.headers.get(
                "X-Apify-Actor-Run-Id"
            )

        (folder / "dataset_response.bin").write_bytes(body)
        manifest["dataset_sha256"] = sha256(body)
        manifest["status"] = "unknown_outcome"
        save_json(path, manifest)

        if manifest["apify_http_status"] not in (200, 201):
            classify_failure(body, token, folder, manifest)
            raise RuntimeError("Apify acquisition failed")

        manifest["status"] = "validation_failed"
        items = json.loads(body)
        if not isinstance(items, list) or len(items) != 1:
            raise ValueError("Expected exactly one provider result")

        item = items[0]
        if not isinstance(item, dict) or item.get("url") != source_url:
            raise ValueError("Requested URL does not match provider result")

        fetch = item.get("fetch", {})
        raw = item.get("raw")
        if not isinstance(fetch, dict) or not isinstance(raw, str):
            raise ValueError("Missing fetch metadata or raw response")

        manifest["fetch"] = fetch

        # Keep the response even when it is an error page.
        raw_bytes = raw.encode("utf-8")
        (folder / "source_response.txt").write_bytes(raw_bytes)
        manifest["source_sha256"] = sha256(raw_bytes)
        manifest["source_representation"] = (
            "UTF-8 encoding of provider raw string"
        )

        final_url = fetch.get("loadedUrl")
        if not isinstance(final_url, str):
            raise ValueError("Missing final URL")

        final = urllib.parse.urlsplit(final_url)
        if (
            final.scheme != "https"
            or final.hostname != "www.bauhaus.info"
        ):
            raise ValueError("Final URL is outside the expected source")

        status = fetch.get("httpStatusCode")
        if type(status) is not int:
            raise ValueError("Missing source HTTP status")

        manifest.update({
            "status": "captured",
            "final_url": final_url,
            "redirect_observed": final_url != source_url,
            "classification": "unreviewed",
            "is_active_store_verified": False,
        })

        print("Source HTTP status:", status, flush=True)
        print("Final URL:", final_url, flush=True)

    except Exception as error:
        if manifest["status"] == "request_pending":
            manifest["status"] = "unknown_outcome"
        manifest["error_type"] = type(error).__name__
        print("Attempt failed:", type(error).__name__, flush=True)

    finally:
        manifest["finished_at_utc"] = utc_now()
        save_json(path, manifest)
        print("Evidence saved:", folder.resolve(), flush=True)

    return folder, manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-new-runs", type=int, default=19)
    parser.add_argument(
        "--reconciliation",
        type=Path,
        default=PROJECT_ROOT / (
            "data/processed/"
            "bauhaus_sitemap_reconciliation_"
            "8c94b25db372426eab0fe772c2b6c554.json"
        ),
    )
    args = parser.parse_args()

    if args.max_new_runs < 0:
        parser.error("--max-new-runs cannot be negative")

    source_bytes = args.reconciliation.read_bytes()
    reconciliation = json.loads(source_bytes)
    targets = {}

    for entry in reconciliation["sitemap_ids_not_in_inventory"]:
        for page in entry["entries"]:
            url = page["url"]
            parsed = urllib.parse.urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "www.bauhaus.info"
                or not parsed.path.startswith("/fc/")
            ):
                raise ValueError("Unexpected sitemap target URL")
            targets[url] = entry["store_id"]

    print("Unmatched sitemap URLs:", len(targets), flush=True)
    for url, store_id in sorted(targets.items()):
        print(store_id, url, flush=True)

    if not args.live:
        print("\nPreview only. No requests made.", flush=True)
        return

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise RuntimeError("APIFY_TOKEN missing; no request made")

    root = PROJECT_ROOT / "data/raw/bauhaus_sitemap_pages"
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / f"collection_{uuid.uuid4().hex}.json"
    report = {
        "started_at_utc": utc_now(),
        "reconciliation": str(args.reconciliation.resolve()),
        "reconciliation_sha256": sha256(source_bytes),
        "max_new_runs": args.max_new_runs,
        "new_run_attempts": 0,
        "status": "running",
        "results": [],
    }

    try:
        for position, (url, store_id) in enumerate(
            sorted(targets.items()), start=1
        ):
            print(
                f"\n[{position}/{len(targets)}] Sitemap ID {store_id}",
                flush=True,
            )
            record = {
                "sitemap_id": store_id,
                "source_url": url,
                "status": "starting",
                "attempts": [],
            }
            report["results"].append(record)
            save_json(report_path, report)

            cached = cached_page(root, url)
            if cached is not None:
                cached_manifest = json.loads(
                    (cached / "manifest.json").read_bytes()
                )
                cached_status = (
                    "unresolved_terminal"
                    if cached_manifest.get("status")
                    == "confirmed_terminal_unresolved"
                    else "cached"
                )
                record.update(
                    status=cached_status,
                    capture=str(cached),
                )
                save_json(report_path, report)
                continue

            for attempt_number in range(1, 4):
                if report["new_run_attempts"] >= args.max_new_runs:
                    record["status"] = "attempt_limit_reached"
                    raise RuntimeError("New-run ceiling reached")

                report["new_run_attempts"] += 1
                save_json(report_path, report)
                folder, manifest = capture_page(url, token, root)
                record["attempts"].append({
                    "capture": str(folder),
                    "status": manifest["status"],
                    "run_id": manifest.get("run_id"),
                })
                save_json(report_path, report)

                if manifest["status"] == "captured":
                    record.update(status="captured", capture=str(folder))
                    break

                if (
                    manifest.get("status")
                    == "confirmed_terminal_unresolved"
                ):
                    record["status"] = "unresolved_terminal"
                    print(
                        "Terminal run failure preserved; "
                        "continuing without another request.",
                        flush=True,
                    )
                    break

                if not (
                    manifest.get("retryable") is True
                    and manifest.get("run_status") == "FAILED"
                    and manifest.get("retry_reason")
                    == "confirmed_failed_upstream_502"
                ):
                    record["status"] = "failed_or_unknown"
                    raise RuntimeError(
                        "Unclassified failure; inspect saved evidence"
                    )

                if attempt_number == 3:
                    record["status"] = "unresolved_after_retries"
                    break

                delay = 30 * attempt_number
                print(f"Retrying in {delay} seconds.", flush=True)
                time.sleep(delay)

            save_json(report_path, report)
            if position < len(targets):
                time.sleep(3)

        report["status"] = "completed"

    except BaseException as error:
        report["status"] = "interrupted_or_failed"
        report["error_type"] = type(error).__name__
        raise

    finally:
        report["finished_at_utc"] = utc_now()
        report["unresolved_urls"] = [
            row["source_url"] for row in report["results"]
            if row["status"] not in {"captured", "cached"}
        ]
        if report["status"] == "completed" and report["unresolved_urls"]:
            report["status"] = "completed_with_unresolved"
        save_json(report_path, report)
        print("Collection report:", report_path.resolve(), flush=True)


if __name__ == "__main__":
    main()