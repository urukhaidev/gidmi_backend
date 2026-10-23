from __future__ import annotations

import argparse
import csv
from difflib import SequenceMatcher
from collections import defaultdict
from pathlib import Path
from typing import Any

import tag_category_linker as linker


DEFAULT_MIN_CITIES = 3
DEFAULT_MIN_SLUG_SIMILARITY = 0.80


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate one-to-one Tripster-tag link proposals using exact and similar slugs."
    )
    parser.add_argument("--out", default="tripster_catalog_slug_proposals.csv")
    parser.add_argument("--min-cities", type=int, default=DEFAULT_MIN_CITIES)
    parser.add_argument("--min-slug-similarity", type=float, default=DEFAULT_MIN_SLUG_SIMILARITY)
    parser.add_argument("--apply-exact", action="store_true", help="Write exact/normalized slug links to tripster_tag_id.")
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args()


def catalog_rows_by_slug(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        slug = str(row.get("sub_slug") or "").strip()
        if slug:
            result.setdefault(slug, []).append(row)
    return result


def shared_tripster_tags(db: linker.SupabaseRest, min_cities: int) -> list[dict[str, Any]]:
    city_ids_by_tag: dict[int, set[int]] = defaultdict(set)
    for row in db.select_all("tripster_city_tags", "tag_id,city_id"):
        if row.get("tag_id") is not None and row.get("city_id") is not None:
            city_ids_by_tag[int(row["tag_id"])].add(int(row["city_id"]))

    tags = db.select_all("tripster_tags", "id,name,slug,category,is_common")
    output: list[dict[str, Any]] = []
    for tag in tags:
        cities_count = len(city_ids_by_tag.get(int(tag["id"]), set()))
        if cities_count >= min_cities and str(tag.get("slug") or "").strip():
            output.append({**tag, "cities_count": cities_count})
    return sorted(output, key=lambda row: (-int(row["cities_count"]), str(row.get("slug") or "")))


def normalize_slug(slug: str) -> str:
    value = slug.lower().strip()
    # Typical transliteration variants in Tripster slugs: peshehodnyie -> peshehodnye.
    for old, new in (("yie", "ye"), ("yi", "y"), ("ii", "i"), ("iy", "i")):
        value = value.replace(old, new)
    return "".join(char for char in value if char.isalnum())


def similar_catalog_slug(tag_slug: str, catalog_by_slug: dict[str, list[dict[str, Any]]], minimum: float) -> tuple[list[dict[str, Any]], float] | None:
    normalized_tag_slug = normalize_slug(tag_slug)
    if not normalized_tag_slug:
        return None
    candidates = [
        (catalogs, SequenceMatcher(None, normalized_tag_slug, normalize_slug(catalog_slug)).ratio())
        for catalog_slug, catalogs in catalog_by_slug.items()
        if normalize_slug(catalog_slug)
    ]
    if not candidates:
        return None
    catalogs, similarity = max(candidates, key=lambda item: item[1])
    return (catalogs, similarity) if similarity >= minimum else None


def proposal_row(tag: dict[str, Any], catalog: dict[str, Any], source: str, similarity: Any) -> dict[str, Any]:
    return {
        "approve": "",
        "tripster_tag_id": tag["id"],
        "tripster_slug": tag.get("slug") or "",
        "tripster_name": tag.get("name") or "",
        "cities_count": tag["cities_count"],
        "tripster_is_common": tag.get("is_common"),
        "catalog_id": catalog.get("id") or "",
        "catalog_sub_slug": catalog.get("sub_slug") or "",
        "catalog_main_name": catalog.get("main_name") or "",
        "catalog_sub_name": catalog.get("sub_name") or "",
        "match_source": source,
        "similarity": similarity if similarity is not None else "",
    }


def apply_exact_links(db: linker.SupabaseRest, proposals: list[dict[str, Any]], timeout: float) -> int:
    applied = 0
    priority = {"exact_slug": 0, "normalized_slug": 1}
    eligible = [proposal for proposal in proposals if proposal["match_source"] in priority]
    selected_by_catalog: dict[str, dict[str, Any]] = {}
    for proposal in sorted(eligible, key=lambda row: priority[row["match_source"]]):
        selected_by_catalog.setdefault(str(proposal["catalog_id"]), proposal)
    for proposal in selected_by_catalog.values():
        linker.request_with_retries(
            "PATCH",
            f"{db.base_url}/tag_category_catalog",
            headers={**db.headers, "Prefer": "return=minimal"},
            params={"id": f"eq.{proposal['catalog_id']}", "tripster_tag_id": "is.null"},
            json={"tripster_tag_id": proposal["tripster_tag_id"]},
            timeout=timeout,
            max_attempts=2,
        )
        applied += 1
    return applied


def main() -> None:
    args = parse_args()
    if args.min_cities < 1:
        raise RuntimeError("--min-cities must be >= 1")
    if not 0 < args.min_slug_similarity <= 1:
        raise RuntimeError("--min-slug-similarity must be in (0, 1]")

    linker.load_dotenv(Path(__file__).resolve().with_name(".env"))
    db = linker.supabase_from_env()
    catalog_rows = db.select_all(
        "tag_category_catalog",
        "id,main_name,sub_name,sub_slug,is_common",
        filters={"is_common": "eq.true"},
        order="sub_slug.asc",
    )
    catalog_by_slug = catalog_rows_by_slug(catalog_rows)
    shared_tags = shared_tripster_tags(db, args.min_cities)

    proposals: list[dict[str, Any]] = []
    for tag in shared_tags:
        tag_slug = str(tag["slug"])
        exact_catalogs = catalog_by_slug.get(tag_slug)
        if exact_catalogs:
            proposals.extend(proposal_row(tag, catalog, "exact_slug", 1.0) for catalog in exact_catalogs)
            continue

        normalized_catalogs = [
            catalog
            for slug, catalogs in catalog_by_slug.items()
            if normalize_slug(slug) == normalize_slug(tag_slug)
            for catalog in catalogs
        ]
        if normalized_catalogs:
            proposals.extend(proposal_row(tag, catalog, "normalized_slug", 1.0) for catalog in normalized_catalogs)
            continue

        similar = similar_catalog_slug(tag_slug, catalog_by_slug, args.min_slug_similarity)
        if similar:
            catalogs, similarity = similar
            proposals.extend(proposal_row(tag, catalog, "similar_slug", round(similarity, 4)) for catalog in catalogs)

    fields = [
        "approve", "tripster_tag_id", "tripster_slug", "tripster_name", "cities_count", "tripster_is_common",
        "catalog_id", "catalog_sub_slug", "catalog_main_name", "catalog_sub_name", "match_source", "similarity",
    ]
    out = Path(args.out)
    with out.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(proposals)
    print(
        f"Wrote {len(proposals)} proposals for {len(shared_tags)} Tripster tags "
        f"(exact={sum(row['match_source'] == 'exact_slug' for row in proposals)}, "
        f"normalized={sum(row['match_source'] == 'normalized_slug' for row in proposals)}, "
        f"similar={sum(row['match_source'] == 'similar_slug' for row in proposals)}) to {out}"
    )
    if args.apply_exact:
        applied = apply_exact_links(db, proposals, args.timeout)
        print(f"Applied exact/normalized links: {applied}")


if __name__ == "__main__":
    main()
