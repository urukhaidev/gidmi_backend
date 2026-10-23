from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


TRIPSTER_EXPERIENCES_URL = "https://experience.tripster.ru/api/web/v2/experiences/"
DEFAULT_TYPES = "private,group,ticket,transfer_service"
DEFAULT_LIMIT = 100
DEFAULT_WORKERS = 40
REST_PAGE_SIZE = 1000
SUPABASE_CITY_PAGE_SIZE = 200
RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

thread_local = threading.local()
print_lock = threading.Lock()
ids_lock = threading.Lock()
unique_experience_ids: set[int] = set()


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
        adapter = requests.adapters.HTTPAdapter(pool_connections=60, pool_maxsize=60)
        current.mount("https://", adapter)
        current.mount("http://", adapter)
        thread_local.session = current
    return current


def request_with_retries(method: str, url: str, *, timeout: float = 45.0, **kwargs: Any) -> requests.Response:
    for attempt in range(1, 6):
        response = session().request(method, url, timeout=timeout, **kwargs)
        if response.status_code not in RETRYABLE_STATUSES:
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                raise requests.HTTPError(f"{exc}; body={response.text[:800]}", response=response) from exc
            return response

        if attempt == 5:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else min(30.0, 2 ** (attempt - 1) + random.random())
        time.sleep(delay)

    raise RuntimeError("request failed")


class SupabaseRest:
    def __init__(self, url: str, key: str, timeout: float) -> None:
        self.base_url = f"{url.rstrip('/')}/rest/v1"
        self.timeout = timeout
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    def select_cities(self) -> list[dict[str, Any]]:
        cities: list[dict[str, Any]] = []
        last_id = 0
        while True:
            response = request_with_retries(
                "GET",
                f"{self.base_url}/cities",
                headers=self.headers,
                params={
                    "select": "id,slug,name_ru,experience_count",
                    "experience_count": "gt.0",
                    "slug": "not.is.null",
                    "id": f"gt.{last_id}",
                    "order": "id.asc",
                    "limit": SUPABASE_CITY_PAGE_SIZE,
                },
                timeout=self.timeout,
            )
            page = response.json()
            if not isinstance(page, list):
                raise RuntimeError(f"Unexpected Supabase cities response: {page!r}")
            cities.extend(page)
            if len(page) < SUPABASE_CITY_PAGE_SIZE:
                return cities
            last_id = int(page[-1]["id"])


def normalize_tripster_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        if isinstance(payload[0].get("results"), list):
            return payload[0]
    raise RuntimeError(f"Unexpected Tripster response shape: {type(payload).__name__}")


def fetch_page(token: str, city_slug: str, next_url: str | None, limit: int, timeout: float) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "ekskursii-bd-count/1.0",
    }
    if next_url:
        response = request_with_retries(
            "GET",
            urljoin(TRIPSTER_EXPERIENCES_URL, next_url),
            headers=headers,
            timeout=timeout,
        )
    else:
        response = request_with_retries(
            "GET",
            TRIPSTER_EXPERIENCES_URL,
            headers=headers,
            params={
                "city__slug": city_slug,
                "persons_count": 1,
                "type": DEFAULT_TYPES,
                "bookable": "all",
                "price_format": "travelers",
                "limit": limit,
                "offset": 0,
                "extended_movement_types": "true",
                "exclude_needs_license": "false",
                "extended": "true",
            },
            timeout=timeout,
        )
    return normalize_tripster_payload(response.json())


def count_city(city: dict[str, Any], token: str, limit: int, timeout: float) -> dict[str, Any]:
    slug = str(city["slug"])
    next_url: str | None = None
    pages = 0
    rows = 0
    reported_count: int | None = None
    city_unique_ids: set[int] = set()

    while True:
        page = fetch_page(token, slug, next_url, limit, timeout)
        results = page.get("results") or []
        if not isinstance(results, list):
            raise RuntimeError(f"Unexpected results for {slug}: {type(results).__name__}")
        if reported_count is None and page.get("count") is not None:
            reported_count = int(page["count"])

        pages += 1
        rows += len(results)
        for item in results:
            if isinstance(item, dict) and item.get("id") is not None:
                city_unique_ids.add(int(item["id"]))

        next_url = page.get("next")
        if not next_url:
            break

    with ids_lock:
        before = len(unique_experience_ids)
        unique_experience_ids.update(city_unique_ids)
        new_unique = len(unique_experience_ids) - before

    return {
        "city_id": city["id"],
        "slug": slug,
        "name_ru": city.get("name_ru"),
        "city_expected_count": city.get("experience_count"),
        "tripster_reported_count": reported_count,
        "fetched_rows": rows,
        "city_unique_ids": len(city_unique_ids),
        "new_global_unique_ids": new_unique,
        "pages": pages,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Count Tripster experiences without writing to Supabase.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--city-slug", action="append")
    parser.add_argument("--max-cities", type=int)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    tripster_token = os.getenv("TRIPSTER_TOKEN")
    if not supabase_url or not supabase_key or not tripster_token:
        raise SystemExit("Set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and TRIPSTER_TOKEN in .env")

    cities = SupabaseRest(supabase_url, supabase_key, args.timeout).select_cities()
    if args.city_slug:
        wanted = {slug.strip() for value in args.city_slug for slug in value.split(",") if slug.strip()}
        cities = [city for city in cities if city.get("slug") in wanted]
    if args.max_cities:
        cities = cities[: args.max_cities]
    if not cities:
        raise SystemExit("No cities selected")

    log(f"COUNT ONLY: cities={len(cities)}, workers={args.workers}, limit={args.limit}")
    started_at = time.time()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(count_city, city, tripster_token, args.limit, args.timeout): city for city in cities}
        done = 0
        for future in as_completed(futures):
            city = futures[future]
            done += 1
            try:
                result = future.result()
                results.append(result)
                log(
                    f"[{done}/{len(cities)}] {result['slug']}: rows={result['fetched_rows']}, "
                    f"city_unique={result['city_unique_ids']}, pages={result['pages']}"
                )
            except Exception as exc:
                errors.append({"city_id": str(city.get("id")), "slug": str(city.get("slug")), "error": str(exc)})
                log(f"[{done}/{len(cities)}] {city.get('slug')}: ERROR: {exc}")

    summary = {
        "cities": len(cities),
        "city_errors": len(errors),
        "sum_tripster_reported_count": sum(int(row["tripster_reported_count"] or 0) for row in results),
        "sum_fetched_rows": sum(int(row["fetched_rows"]) for row in results),
        "sum_city_unique_ids": sum(int(row["city_unique_ids"]) for row in results),
        "global_unique_experience_ids": len(unique_experience_ids),
        "duplicate_city_memberships": sum(int(row["city_unique_ids"]) for row in results) - len(unique_experience_ids),
        "elapsed_seconds": round(time.time() - started_at, 2),
    }

    report = {"summary": summary, "cities": sorted(results, key=lambda row: int(row["city_id"])), "errors": errors}
    output = args.output or f"tripster_experience_count_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    log("Summary:")
    for key, value in summary.items():
        log(f"  {key}: {value}")
    log(f"Report: {Path(output).resolve()}")


if __name__ == "__main__":
    main()
