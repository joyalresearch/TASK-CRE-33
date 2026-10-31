import argparse
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import json
from pathlib import Path
import urllib.request
import urllib.error
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    if not args.run_id.isalnum():
        parser.error("Run ID must contain only letters and digits")

    token = getpass("Apify token (hidden): ").strip()
    if not token:
        raise ValueError("Token is empty")

    folder = Path("data/raw/bauhaus_locator") / uuid.uuid4().hex
    folder.mkdir(parents=True)

    manifest = {
        "run_id": args.run_id,
        "started_at_utc": utc_now(),
        "operation": "retrieve_existing_run",
        "coverage_verified": False,
        "files": [],
        "status": "failed",
    }

    def retrieve(url, filename):
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

        try:
            response = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as error:
            response = error

        with response:
            body = response.read()
            status = response.status
            total = response.headers.get("X-Apify-Pagination-Total")

        (folder / filename).write_bytes(body)
        manifest["files"].append({
            "file": filename,
            "api_url": url,
            "retrieved_at_utc": utc_now(),
            "http_status": status,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        })

        if status != 200:
            raise RuntimeError(f"Apify API returned HTTP {status}")

        return json.loads(body), total

    try:
        result, _ = retrieve(
            f"https://api.apify.com/v2/actor-runs/{args.run_id}",
            "run.json",
        )
        run = result["data"]

        if run["status"] != "SUCCEEDED":
            raise ValueError("The selected run did not succeed")

        dataset_id = run["defaultDatasetId"]
        items, total = retrieve(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items"
            "?format=json&clean=false&offset=0&limit=2",
            "dataset.json",
        )

        # This importer deliberately supports the observed one-item run.
        if total != "1" or not isinstance(items, list) or len(items) != 1:
            raise ValueError("Expected exactly one dataset item")

        item = items[0]
        expected_url = "https://www.bauhaus.info/api/stores?q=10115"

        if (
            item.get("url") != expected_url
            or item.get("fetch", {}).get("loadedUrl") != expected_url
            or item.get("fetch", {}).get("httpStatusCode") != 200
        ):
            raise ValueError("Unexpected source URL or source HTTP status")

        raw = item["raw"]
        if not isinstance(raw, str):
            raise ValueError("Expected the original JSON as a raw string")

        stores = json.loads(raw)
        if not isinstance(stores, list) or not stores:
            raise ValueError("Expected a nonempty store array")

        ids = [store["store_id"] for store in stores]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate official store IDs")

        # Preserve the provider-returned text without reformatting it.
        source_bytes = raw.encode("utf-8")
        (folder / "source_response.json").write_bytes(source_bytes)
        manifest["source_response"] = {
            "file": "source_response.json",
            "representation": "UTF-8 encoding of provider raw string",
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "fetch": item["fetch"],
        }
        manifest["store_count"] = len(stores)
        manifest["status"] = "validated"

        print("Existing run:", args.run_id)
        print("Source HTTP status:", item["fetch"]["httpStatusCode"])
        print("Stores:", len(stores))
        print("Unique official IDs:", len(set(ids)))

    except Exception as error:
        # Avoid printing credentials or unrestricted server error bodies.
        manifest["error_type"] = type(error).__name__
        print("Import failed:", type(error).__name__)
        raise

    finally:
        manifest["finished_at_utc"] = utc_now()
        (folder / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("Evidence saved:", folder.resolve())


if __name__ == "__main__":
    main()