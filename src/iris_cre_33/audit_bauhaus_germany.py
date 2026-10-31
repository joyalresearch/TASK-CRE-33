
import collections
import json
from pathlib import Path

source = Path("data/raw/bauhaus_germany_pois.json")

data = json.loads(source.read_text(encoding="utf-8"))
elements = data["elements"]

ways = [e for e in elements if e["type"] == "way"]
nodes = [e for e in elements if e["type"] == "node"]

print("NATIONWIDE BAUHAUS AUDIT")
print("Total elements:", len(elements))
print("Nodes:", len(nodes))
print("Ways:", len(ways))

building_tags = collections.Counter(
    e.get("tags", {}).get("building", "missing")
    for e in ways
)

print("\nBuilding tags on ways:")
for tag, count in building_tags.most_common():
    print(f"  {tag}: {count}")

print("\nWay geometry availability:")
print(
    "Ways with full geometry:",
    sum("geometry" in e for e in ways),
)
print(
    "Ways with node references:",
    sum("nodes" in e for e in ways),
)

print("\nStore name examples:")
for element in elements[:10]:
    tags = element.get("tags", {})
    print(
        element["type"],
        element["id"],
        "|",
        tags.get("name", "unnamed"),
        "| building:",
        tags.get("building", "missing"),
    )
