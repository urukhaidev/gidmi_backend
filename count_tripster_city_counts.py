from __future__ import annotations

import argparse
import csv
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


TRIPSTER_EXPERIENCES_URL = "https://experience.tripster.ru/api/web/v2/experiences/"
DEFAULT_TYPES = "private,group,ticket,transfer_service"
DEFAULT_WORKERS = 40
SUPABASE_PAGE_SIZE = 200
RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

thread_local = threading.local()
print_lock = threading.Lock()


def log(message: str) -> None:
    with print_lock:
        print(message, flush=True)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def session() -> requests.Session:
    current = getattr(thread_local, "session", None)
    if current is None:
        current = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=80, pool_maxsize=80)
        current.mount("https://", adapter)
        current.mount("http://", adapter)
        thread_local.session = current
    return current


def request_with_retries(method: str, url: str, *, timeout: float, **kwargs: Any) -> requests.Response:
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


def load_cities_from_supabase(timeout: float) -> list[dict[str, Any]]:
    supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_KEY"]
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}

    cities: list[dict[str, Any]] = []
    last_id = 0
    while True:
        response = request_with_retries(
            "GET",
            f"{supabase_url}/rest/v1/cities",
            headers=headers,
            params={
                "select": "id,slug,name_ru,experience_count",
                "experience_count": "gt.0",
                "slug": "not.is.null",
                "id": f"gt.{last_id}",
                "order": "id.asc",
                "limit": SUPABASE_PAGE_SIZE,
            },
            timeout=timeout,
        )
        page = response.json()
        if not isinstance(page, list):
            raise RuntimeError(f"Unexpected Supabase cities response: {page!r}")
        cities.extend(page)
        if len(page) < SUPABASE_PAGE_SIZE:
            return cities
        last_id = int(page[-1]["id"])


def normalize_tripster_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    raise RuntimeError(f"Unexpected Tripster response shape: {type(payload).__name__}")


def fetch_city_count(city: dict[str, Any], token: str, timeout: float) -> dict[str, Any]:
    response = request_with_retries(
        "GET",
        TRIPSTER_EXPERIENCES_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "ekskursii-bd-city-count/1.0",
        },
        params={
            "city__slug": city["slug"],
            "persons_count": 1,
            "type": DEFAULT_TYPES,
            "bookable": "all",
            "price_format": "travelers",
            "limit": 1,
            "offset": 0,
            "extended_movement_types": "true",
            "exclude_needs_license": "false",
            "extended": "true",
        },
        timeout=timeout,
    )
    payload = normalize_tripster_payload(response.json())
    return {
        "city_id": city["id"],
        "slug": city["slug"],
        "name_ru": city.get("name_ru"),
        "stored_experience_count": city.get("experience_count"),
        "tripster_count": int(payload.get("count") or 0),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Tripster count per city without pagination or writes.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--city-slug", action="append", help="One slug or comma-separated slug list.")
    parser.add_argument("--max-cities", type=int)
    parser.add_argument("--output-prefix", default=None)
    return parser.parse_args()


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()

    token = os.environ.get("TRIPSTER_TOKEN")
    if not token:
        raise SystemExit("Set TRIPSTER_TOKEN in .env")

    cities = load_cities_from_supabase(args.timeout)
    if args.city_slug:
        wanted = {slug.strip() for value in args.city_slug for slug in value.split(",") if slug.strip()}
        cities = [city for city in cities if city.get("slug") in wanted]
    if args.max_cities:
        cities = cities[: args.max_cities]
    if not cities:
        raise SystemExit("No cities selected")

    started_at = time.time()
    log(f"COUNT PER CITY: cities={len(cities)}, workers={args.workers}, limit=1")

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch_city_count, city, token, args.timeout): city for city in cities}
        for done, future in enumerate(as_completed(futures), start=1):
            city = futures[future]
            try:
                row = future.result()
                rows.append(row)
                log(f"[{done}/{len(cities)}] {row['slug']}: count={row['tripster_count']}")
            except Exception as exc:
                errors.append({"city_id": str(city.get("id")), "slug": str(city.get("slug")), "error": str(exc)})
                log(f"[{done}/{len(cities)}] {city.get('slug')}: ERROR: {exc}")

    rows.sort(key=lambda row: int(row["city_id"]))
    summary = {
        "cities": len(cities),
        "errors": len(errors),
        "sum_tripster_count": sum(int(row["tripster_count"]) for row in rows),
        "sum_stored_experience_count": sum(int(row["stored_experience_count"] or 0) for row in rows),
        "elapsed_seconds": round(time.time() - started_at, 2),
    }

    prefix = args.output_prefix or f"tripster_city_counts_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    json_path = Path(f"{prefix}.json").resolve()
    csv_path = Path(f"{prefix}.csv").resolve()
    json_path.write_text(
        json.dumps({"summary": summary, "cities": rows, "errors": errors}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["city_id", "slug", "name_ru", "stored_experience_count", "tripster_count"],
        )
        writer.writeheader()
        writer.writerows(rows)

    log("Summary:")
    for key, value in summary.items():
        log(f"  {key}: {value}")
    log(f"JSON: {json_path}")
    log(f"CSV: {csv_path}")


if __name__ == "__main__":
    main()
