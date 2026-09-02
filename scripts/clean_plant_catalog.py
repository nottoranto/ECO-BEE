#!/usr/bin/env python3
"""Keep the imported plant picker Thai-only, number-free and deduplicated."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "reference_catalog.json"
RESEARCH_PATH = ROOT / "data" / "research_plants.json"


def clean_name(value):
    name = " ".join(str(value or "").split())
    return re.sub(r"^\s*(?:\d+\s*)+", "", name).strip()


def main():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    research = json.loads(RESEARCH_PATH.read_text(encoding="utf-8"))
    existing = {clean_name(item["thai_name"]) for item in research["plants"]}
    seen = set()
    plants = []
    for item in catalog.get("plants", []):
        name = clean_name(item.get("thai_name"))
        if not re.search(r"[ก-๙]", name) or re.search(r"[A-Za-z0-9]", name):
            continue
        if name in existing or name in seen:
            continue
        seen.add(name)
        plants.append({**item, "thai_name": name})
    catalog["plants"] = plants
    catalog["metadata"]["catalogue_total_after_import"] = len(existing) + len(plants)
    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Kept {len(plants)} imported Thai plant names")


if __name__ == "__main__":
    main()
