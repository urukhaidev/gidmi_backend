from __future__ import annotations

import argparse
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


TRIPSTER_CITYTAGS_URL = "https://experience.tripster.ru/api/partners/trvl/citytags/"
DEFAULT_WORKERS = 10
REST_PAGE_SIZE = 1000

thread_local = threading.local()
print_lock = threading.Lock()


def log(message: str) -> None:
    with print_lock:
        print(message, flush=True)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def session() -> requests.Session:
    current = getattr(thread_local, "session", None)
    if current is None:
        current = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=30, pool_maxsize=30)
        current.mount("https://", adapter)
        current.mount("http://", adapter)
        thread_local.session = current
    return current


def request_with_retries(method: str, url: str, *, timeout: float = 45.0, **kwargs: Any) -> requests.Response:
    last_error: Exception | None = None

    for attempt in range(1, 6):
        try:
            response = session().request(method, url, timeout=timeout, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response

            if attempt == 5:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(30.0, 2 ** (attempt - 1) + random.random())
            time.sleep(delay)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 5:
                raise
            time.sleep(min(30.0, 2 ** (attempt - 1) + random.random()))

    if last_error:
        raise last_error
    raise RuntimeError("request failed")


def batched(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


class SupabaseRest:
    def __init__(self, url: str, service_role_key: str, timeout: float) -> None:
        self.base_url = f"{url.rstrip('/')}/rest/v1"
        self.timeout = timeout
        self.headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
        }

    def select_all(
        self,
        table: str,
        select: str,
        *,
        filters: dict[str, str] | None = None,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset = 0

        while True:
            params: dict[str, Any] = {"select": select, "limit": REST_PAGE_SIZE, "offset": offset}
            if filters:
                params.update(filters)
            if order:
                params["order"] = order

            response = request_with_retries(
                "GET",
                f"{self.base_url}/{table}",
                headers=self.headers,
                params=params,
                timeout=self.timeout,
            )
            page = response.json()
            if not isinstance(page, list):
                raise RuntimeError(f"Unexpected Supabase response for {table}: {page!r}")

            result.extend(page)
            if len(page) < REST_PAGE_SIZE:
                return result
            offset += REST_PAGE_SIZE

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
        if not rows:
            return

        headers = {**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
        for batch in batched(rows, 500):
            request_with_retries(
                "POST",
                f"{self.base_url}/{table}",
                headers=headers,
                params={"on_conflict": on_conflict},
                json=batch,
                timeout=self.timeout,
            )


def normalize_tripster_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        if isinstance(payload[0].get("results"), list):
            return payload[0]
    raise RuntimeError(f"Unexpected Tripster response shape: {type(payload).__name__}")


def map_citytag(item: dict[str, Any], city_id: int) -> tuple[dict[str, Any], dict[str, Any]] | None:
    tag_id = item.get("id")
    citytag_id = item.get("citytag_id")
    if tag_id is None or citytag_id is None:
        return None

    nested_tag = item.get("tag") or {}
    image = item.get("image") or {}
    tag_row = {
        "id": tag_id,
        "name": nested_tag.get("name") or item.get("name"),
        "slug": nested_tag.get("slug") or item.get("slug"),
        "category": nested_tag.get("category") or item.get("category"),
        "is_common": nested_tag.get("is_common", False),
        "is_auto": nested_tag.get("is_auto", False),
        "is_seo_only": nested_tag.get("is_seo_only", False),
        "is_attraction": nested_tag.get("is_attraction", False),
    }
    citytag_row = {
        "citytag_id": citytag_id,
        "tag_id": tag_id,
        "city_id": city_id,
        "name": item.get("name") or nested_tag.get("name"),
        "slug": item.get("slug"),
        "category": item.get("category") or nested_tag.get("category"),
        "url": item.get("url"),
        "is_hidden": item.get("is_hidden"),
        "experience_count": item.get("experience_count"),
        "excursions_count": item.get("excursions_count"),
        "tours_count": item.get("tours_count"),
        "review_count": item.get("review_count"),
        "image_thumbnail": image.get("thumbnail"),
        "image_medium": image.get("medium"),
        "title": item.get("title"),
        "header": item.get("header"),
    }
    return tag_row, citytag_row


def fetch_citytags_page(token: str, city_id: int, next_url: str | None, timeout: float) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "ekskursii-bd-citytags-import/1.0",
    }

    if next_url:
        response = request_with_retries(
            "GET",
            urljoin(TRIPSTER_CITYTAGS_URL, next_url),
            headers=headers,
            timeout=timeout,
        )
    else:
        response = request_with_retries(
            "GET",
            TRIPSTER_CITYTAGS_URL,
            headers=headers,
            params={"city": city_id},
            timeout=timeout,
        )

    return normalize_tripster_payload(response.json())


def import_citytags_for_city(
    db: SupabaseRest,
    city: dict[str, Any],
    token: str,
    timeout: float,
    dry_run: bool,
) -> tuple[int, int]:
    city_id = int(city["id"])
    slug = city.get("slug") or str(city_id)
    next_url: str | None = None
    pages = imported = 0

    while True:
        page = fetch_citytags_page(token, city_id, next_url, timeout)
        results = page.get("results") or []
        if not isinstance(results, list):
            raise RuntimeError(f"Unexpected results value for city {city_id}: {type(results).__name__}")

        mapped_rows = [row for item in results if (row := map_citytag(item, city_id)) is not None]
        if not dry_run:
            tag_rows_by_id = {int(tag_row["id"]): tag_row for tag_row, _ in mapped_rows}
            citytag_rows = [citytag_row for _, citytag_row in mapped_rows]
            db.upsert("tripster_tags", list(tag_rows_by_id.values()), "id")
            db.upsert("tripster_city_tags", citytag_rows, "citytag_id")

        pages += 1
        imported += len(mapped_rows)
        next_url = page.get("next")
        log(f"{slug}: page={pages}, fetched={len(results)}, imported={len(mapped_rows)}, next={'yes' if next_url else 'no'}")
        if not next_url:
            return pages, imported


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Tripster citytags into Supabase tags.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--city-id", action="append", help="One city id or comma-separated city id list.")
    parser.add_argument("--city-slug", action="append", help="One slug or comma-separated slug list.")
    parser.add_argument("--max-cities", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    tripster_token = os.getenv("TRIPSTER_TOKEN")

    if not supabase_url:
        raise SystemExit("Set SUPABASE_URL in environment or .env")
    if not supabase_key:
        raise SystemExit("Set SUPABASE_SERVICE_ROLE_KEY in environment or .env")
    if not tripster_token:
        raise SystemExit("Set TRIPSTER_TOKEN in environment or .env")

    db = SupabaseRest(supabase_url, supabase_key, args.timeout)
    cities = db.select_all(
        "cities",
        "id,slug,name_ru,experience_count",
        filters={"experience_count": "gt.0"},
        order="id.asc",
    )

    if args.city_id:
        wanted_ids = {int(city_id.strip()) for value in args.city_id for city_id in value.split(",") if city_id.strip()}
        cities = [city for city in cities if int(city["id"]) in wanted_ids]
    if args.city_slug:
        wanted_slugs = {slug.strip() for value in args.city_slug for slug in value.split(",") if slug.strip()}
        cities = [city for city in cities if city.get("slug") in wanted_slugs]
    if args.max_cities:
        cities = cities[: args.max_cities]
    if not cities:
        raise SystemExit("No cities selected")

    mode = "DRY RUN" if args.dry_run else "IMPORT"
    log(f"{mode}: cities={len(cities)}, workers={args.workers}")

    total_pages = total_imported = total_errors = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(import_citytags_for_city, db, city, tripster_token, args.timeout, args.dry_run): city
            for city in cities
        }
        for future in as_completed(futures):
            city = futures[future]
            try:
                pages, imported = future.result()
            except Exception as exc:
                total_errors += 1
                log(f"{city.get('slug') or city.get('id')}: ERROR: {exc}")
                continue
            total_pages += pages
            total_imported += imported

    log(f"Done: pages={total_pages}, imported={total_imported}, city_errors={total_errors}")


if __name__ == "__main__":
    main()
