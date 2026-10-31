import argparse
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    """Write metadata atomically."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_api(url, token):
    """Read existing Apify run information without starting a run."""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def classify_failure(body, token, folder, manifest):
    """Allow retries only for a confirmed FAILED upstream-502 run."""
    manifest["retryable"] = False

    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return

        error = payload.get("error", {})
        if not isinstance(error, dict):
            return

        manifest["apify_error_type"] = error.get("type")

        if error.get("type") != "run-failed":
            # Do not retry authentication, credit-limit, or unknown errors.
            return

        message = str(error.get("message", ""))
        match = re.search(r"run ID:\s*([A-Za-z0-9]+)", message)
        run_id = manifest.get("run_id") or (
            match.group(1) if match else None
        )

        if not isinstance(run_id, str):
            return
        if not re.fullmatch(r"[A-Za-z0-9]+", run_id):
            return

        manifest["run_id"] = run_id
        base_url = f"https://api.apify.com/v2/actor-runs/{run_id}"

        run_bytes = read_api(base_url, token)
        (folder / "run.json").write_bytes(run_bytes)
        manifest["run_sha256"] = hashlib.sha256(run_bytes).hexdigest()

        run = json.loads(run_bytes)["data"]
        if run.get("id") != run_id:
            return

        manifest["run_status"] = run.get("status")

        if run.get("status") in {"TIMED-OUT", "ABORTED"}:
            manifest["status"] = "confirmed_terminal_unresolved"
            manifest["retryable"] = False
            return

        if run.get("status") != "FAILED":
            return

        manifest["status"] = "confirmed_failed"

        log_bytes = read_api(base_url + "/log", token)
        (folder / "run.log").write_bytes(log_bytes)
        manifest["run_log_sha256"] = hashlib.sha256(
            log_bytes
        ).hexdigest()

        log = log_bytes.decode("utf-8", errors="replace")
        upstream_failure = False
        upstream_502 = False

        for line in log.splitlines():
            if manifest["source_url"] not in line:
                continue

            if '"UPSTREAM_FETCH_ERROR"' in line:
                upstream_failure = True

            if re.search(r'"statusCode"\s*:\s*502\b', line):
                upstream_502 = True

        if upstream_failure and upstream_502:
            manifest["retryable"] = True
            manifest["retry_reason"] = "confirmed_failed_upstream_502"

    except Exception as error:
        # Incomplete inspection must never authorize another POST.
        manifest["failure_inspection_error_type"] = type(error).__name__


def build_query(args, parser):
    """Build either an observed postcode or coordinate locator query."""
    postcode = args.postcode
    has_coordinates = args.lat is not None or args.lng is not None

    if postcode is not None:
        if has_coordinates:
            parser.error("Use either --postcode or --lat and --lng")

        if not (
            len(postcode) == 5
            and postcode.isascii()
            and postcode.isdigit()
        ):
            parser.error("Postcode must contain five ASCII digits")

        source_url = (
            "https://www.bauhaus.info/api/stores?"
            + urllib.parse.urlencode({"q": postcode})
        )
        return (
            source_url,
            postcode,
            {"type": "postcode", "postcode": postcode},
        )

    if args.lat is None or args.lng is None:
        parser.error("Supply --postcode or both --lat and --lng")

    if not (
        math.isfinite(args.lat)
        and math.isfinite(args.lng)
        and -90 <= args.lat <= 90
        and -180 <= args.lng <= 180
    ):
        parser.error("Coordinates must be finite and within valid ranges")

    source_url = (
        "https://www.bauhaus.info/api/stores-search?"
        + urllib.parse.urlencode({
            "lat": args.lat,
            "lng": args.lng,
            "user_lat": "undefined",
            "user_lng": "undefined",
        })
    )
    return (
        source_url,
        "coordinates",
        {
            "type": "coordinates",
            "lat": args.lat,
            "lng": args.lng,
        },
    )


def extract_stores(raw, query_type):
    """Parse the source schema without changing the saved raw response."""
    payload = json.loads(raw)
    initial = None

    if query_type == "coordinates":
        if not isinstance(payload, dict):
            raise ValueError(
                "Expected a coordinate-search response object"
            )

        stores = payload.get("stores")
        initial = payload.get("initial")
    else:
        stores = payload

    if not isinstance(stores, list):
        raise ValueError("Expected a store array")

    if any(not isinstance(store, dict) for store in stores):
        raise ValueError("Store records must be objects")

    ids = [store["store_id"] for store in stores]

    if any(type(store_id) is not int for store_id in ids):
        raise ValueError("Unexpected store ID type")

    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate official store IDs")

    return stores, initial


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Capture one official BAUHAUS locator response via Apify "
            "using a postcode or coordinates."
        )
    )
    parser.add_argument("--postcode")
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lng", type=float)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Start one Apify run using the existing allowance.",
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
    parser.add_argument(
        "--capture-id",
        help="Optional unique identifier supplied by the collector.",
    )
    args = parser.parse_args()

    source_url, capture_prefix, query_details = build_query(args, parser)

    if not args.live:
        parser.error("No request made. Add --live for one acquisition.")

    capture_id = args.capture_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]+", capture_id):
        parser.error("Invalid capture identifier")

    load_dotenv(args.env_file.resolve(), override=False)

    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        token = getpass("Apify token (hidden): ").strip()
    if not token:
        raise ValueError("Apify token is empty")

    actor_input = {
        "url": source_url,
        "formats": ["raw"],
    }
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

    folder = (
        args.output_dir.resolve()
        / f"{capture_prefix}_{capture_id}"
    )
    folder.mkdir(parents=True, exist_ok=False)

    manifest = {
        "started_at_utc": utc_now(),
        "source_url": source_url,
        "postcode": args.postcode,
        "query": query_details,
        "actor": "apify/web-fetch",
        "api_url": api_url,
        "actor_input": actor_input,
        "python_post_retries": 0,
        "provider_internal_retries": "provider-controlled",
        "coverage_verified": False,
        "retryable": False,
        "status": "prepared",
    }
    manifest_path = folder / "manifest.json"
    save_json(manifest_path, manifest)

    try:
        request = urllib.request.Request(
            api_url,
            data=json.dumps(actor_input).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        # A timeout or crash may leave a remote run active.
        # Never automatically resend this POST inside the fetcher.
        manifest["status"] = "request_pending"
        save_json(manifest_path, manifest)

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
        manifest["dataset_sha256"] = hashlib.sha256(body).hexdigest()

        manifest["status"] = "unknown_outcome"
        save_json(manifest_path, manifest)

        if manifest["apify_http_status"] not in (200, 201):
            classify_failure(body, token, folder, manifest)
            raise RuntimeError(
                f"Apify HTTP {manifest['apify_http_status']}"
            )

        # An Apify success still requires source validation.
        manifest["status"] = "validation_failed"

        items = json.loads(body)
        if not isinstance(items, list) or len(items) != 1:
            raise ValueError("Expected exactly one fetch-result item")

        item = items[0]
        if not isinstance(item, dict):
            raise ValueError("Fetch-result item is not an object")

        fetch = item.get("fetch", {})
        if not isinstance(fetch, dict):
            raise ValueError("Missing fetch metadata")

        manifest["fetch"] = fetch

        if (
            item.get("url") != source_url
            or fetch.get("loadedUrl") != source_url
            or fetch.get("httpStatusCode") != 200
            or not str(fetch.get("contentType", "")).lower().startswith(
                "application/json"
            )
        ):
            raise ValueError(
                "Source URL, status, or content type failed"
            )

        raw = item.get("raw")
        if not isinstance(raw, str):
            raise ValueError("Missing raw source JSON")

        # Preserve the entire source representation, including radius metadata.
        source_bytes = raw.encode("utf-8")
        (folder / "source_response.json").write_bytes(source_bytes)
        manifest["source_sha256"] = hashlib.sha256(
            source_bytes
        ).hexdigest()
        manifest["source_representation"] = (
            "UTF-8 encoding of provider raw string"
        )

        stores, initial = extract_stores(raw, query_details["type"])

        if query_details["type"] == "coordinates":
            manifest["locator_initial"] = initial

        manifest["store_count"] = len(stores)

        if not stores:
            # Preserve an empty response without treating it as proof
            # that no store exists in the surrounding area.
            manifest["status"] = "validated_empty"
            print("Source acquisition: VALID EMPTY RESPONSE", flush=True)
        else:
            manifest["status"] = "validated"
            print("Fresh acquisition: PASSED", flush=True)

        print("Query:", query_details, flush=True)
        print("Source HTTP status:", fetch["httpStatusCode"], flush=True)
        print("Source fetch time:", fetch.get("loadedTime"), flush=True)
        print("Stores:", len(stores), flush=True)
        print(
            "Unique official IDs:",
            len({store["store_id"] for store in stores}),
            flush=True,
        )

        if query_details["type"] == "coordinates":
            print("Locator initial metadata:", initial, flush=True)

        # The existing postcode collector accepts nonempty captures only.
        if not stores:
            raise ValueError(
                "Empty source response preserved for review; "
                "not accepted as a nonempty inventory capture"
            )

    except BaseException as error:
        if manifest["status"] == "request_pending":
            manifest["status"] = "unknown_outcome"

        manifest["error_type"] = type(error).__name__
        print("Acquisition stopped:", type(error).__name__, flush=True)

        if manifest.get("retryable"):
            print(
                "Confirmed upstream 502; eligible for bounded "
                "collector retry.",
                flush=True,
            )
        else:
            print(
                "Automatic retry not permitted; inspect saved evidence.",
                flush=True,
            )
        raise

    finally:
        manifest["finished_at_utc"] = utc_now()
        save_json(manifest_path, manifest)
        print("Evidence saved:", folder.resolve(), flush=True)


if __name__ == "__main__":
    main()