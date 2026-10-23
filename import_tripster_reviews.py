from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


TRIPSTER_REVIEWS_URL = "https://experience.tripster.ru/api/partners/{partner}/experiences/{experience_id}/reviews/"
DEFAULT_PARTNER = "trvl"
DEFAULT_LIMIT = 10
DEFAULT_WORKERS = 10
REST_PAGE_SIZE = 1000
UPSERT_CHUNK_SIZE = 500
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
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = session().request(method, url, timeout=timeout, **kwargs)
            if response.status_code not in RETRYABLE_STATUSES:
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    raise requests.HTTPError(f"{exc}; body={response.text[:1000]}", response=response) from exc
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


def parse_int_set(values: list[str] | None) -> set[int] | None:
    if not values:
        return None
    return {int(part.strip()) for value in values for part in value.split(",") if part.strip()}


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
        rows: list[dict[str, Any]] = []
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
            rows.extend(page)
            if len(page) < REST_PAGE_SIZE:
                return rows
            last_id = int(page[-1]["id"])

    def select_experience_ids(
        self,
        *,
        city_ids: set[int] | None,
        experience_ids: set[int] | None,
        max_rows: int | None,
        active_only: bool = False,
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
            if active_only:
                params["status"] = "eq.active"
            if city_ids:
                if len(city_ids) == 1:
                    params["city_id"] = f"eq.{next(iter(city_ids))}"
                else:
                    params["city_id"] = f"in.({','.join(str(city_id) for city_id in sorted(city_ids))})"
            if experience_ids:
                params["id"] = f"in.({','.join(str(experience_id) for experience_id in sorted(experience_ids))})"

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
            if len(page) < limit or experience_ids:
                return ids
            last_id = int(page[-1]["id"])

    def upsert_reviews(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        headers = {**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
        for batch in batched(rows, UPSERT_CHUNK_SIZE):
            request_with_retries(
                "POST",
                f"{self.base_url}/experience_reviews",
                headers=headers,
                params={"on_conflict": "id"},
                json=batch,
                timeout=self.timeout,
            )


def normalize_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        if isinstance(payload[0].get("results"), list):
            return payload[0]
    raise RuntimeError(f"Unexpected Tripster reviews response: {type(payload).__name__}")


def map_review(item: dict[str, Any], fallback_experience_id: int) -> dict[str, Any] | None:
    review_id = item.get("id")
    if review_id is None:
        return None

    experience = item.get("experience") or {}
    avatar = item.get("avatar") or {}
    photos = item.get("photos") if isinstance(item.get("photos"), list) else []
    simplified_photos = [
        {
            "id": photo.get("id"),
            "small": photo.get("small"),
            "medium": photo.get("medium"),
        }
        for photo in photos
        if isinstance(photo, dict)
    ]

    experience_id = experience.get("id") or fallback_experience_id
    return {
        "id": int(review_id),
        "experience_id": int(experience_id),
        "author_name": item.get("name"),
        "avatar_small_url": avatar.get("small"),
        "avatar_medium_url": avatar.get("medium"),
        "created_at": item.get("created_at"),
        "created_on": item.get("created_on"),
        "created_on_dt": item.get("created_on_dt"),
        "rating": item.get("rating"),
        "text": item.get("text"),
        "guide_reply": item.get("guide_reply"),
        "traveler_level": item.get("traveler_level"),
        "experience_title": experience.get("title"),
        "experience_url": experience.get("url"),
        "experience_is_bookable": experience.get("is_bookable"),
        "experience_is_visible": experience.get("is_visible"),
        "experience_type": experience.get("type"),
        "experience_format": experience.get("format"),
        "photos": simplified_photos,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_reviews(
    *,
    token: str,
    partner: str,
    experience_id: int,
    limit: int,
    timeout: float,
) -> tuple[int, list[dict[str, Any]]]:
    response = request_with_retries(
        "GET",
        TRIPSTER_REVIEWS_URL.format(partner=partner, experience_id=experience_id),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "ekskursii-bd-review-import/1.0",
        },
        params={"limit": limit},
        timeout=timeout,
    )
    payload = normalize_payload(response.json())
    results = payload.get("results") or []
    if not isinstance(results, list):
        raise RuntimeError(f"Unexpected reviews results for {experience_id}: {type(results).__name__}")
    rows = [row for item in results if isinstance(item, dict) and (row := map_review(item, experience_id)) is not None]
    return int(payload.get("count") or 0), rows[:limit]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import latest Tripster reviews into Supabase.")
    parser.add_argument("--partner", default=DEFAULT_PARTNER)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Latest reviews per experience.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-rows", type=int, help="Max experiences to process.")
    parser.add_argument("--id", action="append", help="One experience id or comma-separated ids.")
    parser.add_argument("--city-id", action="append", help="One city id or comma-separated city id list.")
    parser.add_argument("--city-slug", action="append", help="One city slug or comma-separated slug list.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--active-only", action="store_true", help="Select only experiences with status=active.")
    parser.add_argument("--resume", action="store_true", help="Continue from the saved reviews checkpoint.")
    parser.add_argument("--checkpoint", default=str(Path(__file__).with_name("tripster_reviews_checkpoint.json")))
    return parser.parse_args()


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()
    if min(args.limit, args.workers, args.batch_size, args.timeout) <= 0 or (args.max_rows is not None and args.max_rows <= 0):
        raise SystemExit("Limits, workers, batch size and timeout must be positive")

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    tripster_token = os.getenv("TRIPSTER_TOKEN")
    if not supabase_url or not supabase_key or not tripster_token:
        raise SystemExit("Set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and TRIPSTER_TOKEN in .env")

    db = SupabaseRest(supabase_url, supabase_key, args.timeout)

    selected_city_ids = parse_int_set(args.city_id)
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

    selected_experience_ids = parse_int_set(args.id)
    experience_ids = db.select_experience_ids(
        city_ids=selected_city_ids,
        experience_ids=selected_experience_ids,
        max_rows=args.max_rows,
        active_only=args.active_only,
    )
    if not experience_ids:
        log("No experiences selected")
        return

    checkpoint_path = Path(args.checkpoint)
    checkpoint = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "partner": args.partner, "limit": args.limit, "active_only": args.active_only,
        "experience_ids": experience_ids, "completed": [], "imported_reviews": 0,
    }
    if args.resume:
        if not checkpoint_path.exists():
            raise SystemExit("Reviews checkpoint not found")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if any(checkpoint.get(key) != value for key, value in {
            "partner": args.partner, "limit": args.limit, "active_only": args.active_only,
            "experience_ids": experience_ids,
        }.items()):
            raise SystemExit("Reviews checkpoint scope or options differ from this run")
    completed_ids = set(checkpoint["completed"])
    experience_ids = [experience_id for experience_id in experience_ids if experience_id not in completed_ids]

    def save_checkpoint() -> None:
        if not args.dry_run:
            checkpoint["completed"] = sorted(completed_ids)
            temporary_path = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
            temporary_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            temporary_path.replace(checkpoint_path)

    save_checkpoint()

    mode = "DRY RUN" if args.dry_run else "IMPORT"
    log(
        f"{mode}: experiences={len(experience_ids)}, partner={args.partner}, "
        f"limit={args.limit}, workers={args.workers}, active_only={args.active_only}, already_completed={len(completed_ids)}"
    )

    started_at = time.monotonic()
    imported_reviews = total_reviews_available = errors = processed = 0
    pending_rows: list[dict[str, Any]] = []
    pending_ids: list[int] = []

    def flush_pending() -> None:
        if not args.dry_run:
            db.upsert_reviews(pending_rows)
            completed_ids.update(pending_ids)
            checkpoint["imported_reviews"] += len(pending_rows)
            save_checkpoint()
        pending_rows.clear()
        pending_ids.clear()

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                fetch_reviews,
                token=tripster_token,
                partner=args.partner,
                experience_id=experience_id,
                limit=args.limit,
                timeout=args.timeout,
            ): experience_id
            for experience_id in experience_ids
        }

        for done, future in enumerate(as_completed(futures), start=1):
            experience_id = futures[future]
            try:
                total_count, rows = future.result()
                pending_rows.extend(rows)
                pending_ids.append(experience_id)
                total_reviews_available += total_count
                imported_reviews += len(rows)
                processed += 1
                log(f"[{done}/{len(experience_ids)}] {experience_id}: OK reviews={len(rows)} total={total_count}")
            except Exception as exc:
                errors += 1
                log(f"[{done}/{len(experience_ids)}] {experience_id}: ERROR: {exc}")
                continue

            if len(pending_rows) >= args.batch_size or len(pending_ids) >= 100:
                flush_pending()
                log(f"[{done}/{len(experience_ids)}] saved_reviews={imported_reviews}, errors={errors}")

    if pending_ids:
        flush_pending()
    if not errors:
        checkpoint["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_checkpoint()

    elapsed_seconds = max(time.monotonic() - started_at, 0.001)
    speed = processed / elapsed_seconds * 60
    log(
        f"Done: experiences={processed}, imported_reviews={imported_reviews}, "
        f"available_reviews={total_reviews_available}, errors={errors}, "
        f"elapsed={elapsed_seconds:.1f}s, speed={speed:.2f} exp/min"
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
