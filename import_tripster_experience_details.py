from __future__ import annotations

import argparse
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


TRIPSTER_DETAIL_URL = "https://experience.tripster.ru/api/web/v2/experiences/{experience_id}/"
DEFAULT_WORKERS = 20
REST_PAGE_SIZE = 1000
UPSERT_CHUNK_SIZE = 200
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
        adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50)
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


def batched(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


class SupabaseRest:
    def __init__(self, url: str, key: str, timeout: float) -> None:
        self.base_url = f"{url.rstrip('/')}/rest/v1"
        self.timeout = timeout
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def select_cities(self) -> list[dict[str, Any]]:
        cities: list[dict[str, Any]] = []
        last_id = 0
        while True:
            response = request_with_retries(
                "GET",
                f"{self.base_url}/cities",
                headers=self.headers,
                params={
                    "select": "id,slug,name_ru",
                    "id": f"gt.{last_id}",
                    "order": "id.asc",
                    "limit": REST_PAGE_SIZE,
                },
                timeout=self.timeout,
            )
            page = response.json()
            if not isinstance(page, list):
                raise RuntimeError(f"Unexpected Supabase cities response: {page!r}")
            cities.extend(page)
            if len(page) < REST_PAGE_SIZE:
                return cities
            last_id = int(page[-1]["id"])

    def select_experience_ids(
        self,
        missing_only: bool,
        max_rows: int | None,
        city_ids: set[int] | None = None,
    ) -> list[int]:
        ids: list[int] = []
        last_id = 0
        while True:
            limit = REST_PAGE_SIZE
            if max_rows is not None:
                limit = min(limit, max_rows - len(ids))
                if limit <= 0:
                    return ids

            params: dict[str, Any] = {
                "select": "id",
                "id": f"gt.{last_id}",
                "order": "id.asc",
                "limit": limit,
            }
            if city_ids:
                if len(city_ids) == 1:
                    params["city_id"] = f"eq.{next(iter(city_ids))}"
                else:
                    params["city_id"] = f"in.({','.join(str(city_id) for city_id in sorted(city_ids))})"
            if missing_only:
                params["description_html"] = "is.null"

            response = request_with_retries(
                "GET",
                f"{self.base_url}/experiences",
                headers=self.headers,
                params=params,
                timeout=self.timeout,
            )
            page = response.json()
            if not isinstance(page, list):
                raise RuntimeError(f"Unexpected Supabase experiences response: {page!r}")
            ids.extend(int(row["id"]) for row in page)
            if len(page) < limit:
                return ids
            last_id = int(page[-1]["id"])

    def update_experience_details(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return

        headers = {**self.headers, "Prefer": "return=minimal"}
        for row in rows:
            experience_id = row["id"]
            payload = {key: value for key, value in row.items() if key != "id"}
            request_with_retries(
                "PATCH",
                f"{self.base_url}/experiences",
                headers=headers,
                params={"id": f"eq.{experience_id}"},
                json=payload,
                timeout=self.timeout,
            )


def normalize_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    raise RuntimeError(f"Unexpected detail response shape: {type(payload).__name__}")


def map_detail(item: dict[str, Any]) -> dict[str, Any]:
    schedule = item.get("schedule")
    return {
        "id": int(item["id"]),
        "description_html": item.get("description"),
        "meeting_point_text": (item.get("meeting_point") or {}).get("text"),
        "finish_point_text": (item.get("finish_point") or {}).get("text"),
        "video_id": item.get("video_id"),
        "photos_count": item.get("photos_count"),
        "cover_image_url": item.get("cover_image"),
        "cover_image_source": item.get("cover_image_source"),
        "schedule": schedule,
        "schedule_text": schedule.get("text") if isinstance(schedule, dict) else None,
        "languages": item.get("languages"),
        "legality_description": item.get("legality_description"),
        "options": item.get("options"),
        "integration": item.get("integration"),
        "detail_synced_at": datetime.now(timezone.utc).isoformat(),
        # "raw_detail_payload": item,
    }


def fetch_detail(experience_id: int, token: str, timeout: float) -> dict[str, Any]:
    response = request_with_retries(
        "GET",
        TRIPSTER_DETAIL_URL.format(experience_id=experience_id),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "ekskursii-bd-detail-import/1.0",
        },
        params={"detailed": "true"},
        timeout=timeout,
    )
    return map_detail(normalize_payload(response.json()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import detailed Tripster fields into experiences.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--id", action="append", help="One experience id or comma-separated ids.")
    parser.add_argument("--city-id", action="append", help="One city id or comma-separated city id list.")
    parser.add_argument("--city-slug", action="append", help="One city slug or comma-separated slug list.")
    parser.add_argument("--all", action="store_true", help="Refresh all experiences, not only missing descriptions.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    tripster_token = os.getenv("TRIPSTER_TOKEN")
    if not supabase_url or not supabase_key or not tripster_token:
        raise SystemExit("Set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and TRIPSTER_TOKEN in .env")

    db = SupabaseRest(supabase_url, supabase_key, args.timeout)
    if args.id:
        experience_ids = [
            int(experience_id.strip())
            for value in args.id
            for experience_id in value.split(",")
            if experience_id.strip()
        ]
    else:
        selected_city_ids: set[int] | None = None
        if args.city_id:
            selected_city_ids = {
                int(city_id.strip())
                for value in args.city_id
                for city_id in value.split(",")
                if city_id.strip()
            }
        if args.city_slug:
            requested_slugs = {
                slug.strip()
                for value in args.city_slug
                for slug in value.split(",")
                if slug.strip()
            }
            matched_cities = [city for city in db.select_cities() if city.get("slug") in requested_slugs]
            matched_slugs = {str(city.get("slug")) for city in matched_cities}
            missing_slugs = sorted(requested_slugs - matched_slugs)
            if missing_slugs:
                raise SystemExit(f"Unknown city slug(s): {', '.join(missing_slugs)}")
            slug_city_ids = {int(city["id"]) for city in matched_cities}
            selected_city_ids = slug_city_ids if selected_city_ids is None else selected_city_ids & slug_city_ids
        if selected_city_ids is not None and not selected_city_ids:
            raise SystemExit("No cities selected")

        experience_ids = db.select_experience_ids(
            missing_only=not args.all,
            max_rows=args.max_rows,
            city_ids=selected_city_ids,
        )

    if not experience_ids:
        log("No experiences selected")
        return

    mode = "DRY RUN" if args.dry_run else "IMPORT"
    log(f"{mode}: details={len(experience_ids)}, workers={args.workers}")

    imported = errors = 0
    pending_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch_detail, experience_id, tripster_token, args.timeout): experience_id for experience_id in experience_ids}
        for done, future in enumerate(as_completed(futures), start=1):
            experience_id = futures[future]
            try:
                row = future.result()
                pending_rows.append(row)
                imported += 1
            except Exception as exc:
                errors += 1
                log(f"[{done}/{len(experience_ids)}] {experience_id}: ERROR: {exc}")
                continue

            if len(pending_rows) >= args.batch_size:
                if not args.dry_run:
                    db.update_experience_details(pending_rows)
                log(f"[{done}/{len(experience_ids)}] saved={imported}, errors={errors}")
                pending_rows = []

    if pending_rows and not args.dry_run:
        db.update_experience_details(pending_rows)

    log(f"Done: imported={imported}, errors={errors}")


if __name__ == "__main__":
    main()
