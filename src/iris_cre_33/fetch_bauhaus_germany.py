
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUTPUT = Path("data/raw/bauhaus_germany_pois.json")

QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="DE"][admin_level=2]->.germany;
(
  nwr["shop"="doityourself"]["brand"~"BAUHAUS",i](area.germany);
  nwr["shop"="doityourself"]["name"~"BAUHAUS",i](area.germany);
);
out center tags;
"""

ENDPOINTS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
]


def fetch():
    payload = urllib.parse.urlencode({"data": QUERY}).encode()

    for endpoint in ENDPOINTS:
        print(f"Trying {endpoint}", flush=True)

        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "User-Agent": "IRIS-CRE-33-research/0.1",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        try:
            with urllib.request.urlopen(
                request, timeout=240
            ) as response:
                data = json.load(response)

            elements = data.get("elements")
            if not isinstance(elements, list):
                raise ValueError("No elements array in response")

            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            print(f"Downloaded {len(elements)} OSM elements")
            print(f"Saved to {OUTPUT}")
            return

        except Exception as exc:
            print(f"Endpoint failed: {exc}")
            time.sleep(3)

    raise RuntimeError("All Overpass endpoints failed")


if __name__ == "__main__":
    fetch()
