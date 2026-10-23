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
from urllib.parse import urljoin

import requests

from import_tripster_citytags import fetch_citytags_page, map_citytag


TRIPSTER_EXPERIENCES_URL = "https://experience.tripster.ru/api/web/v2/experiences/"
DEFAULT_TYPES = "private,group,ticket,transfer_service"
DEFAULT_LIMIT = 100
DEFAULT_WORKERS = 25
REST_PAGE_SIZE = 1000
UPSERT_CHUNK_SIZE = 100
INSERT_CHUNK_SIZE = 2000
DELETE_ID_CHUNK_SIZE = 500
MAX_PHOTOS_PER_EXPERIENCE = 5
RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

thread_local = threading.local()
print_lock = threading.Lock()
write_lock = threading.Semaphore(4)


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
        retryable = True
        try:
            response = session().request(method, url, timeout=timeout, **kwargs)
            retryable = response.status_code in RETRYABLE_STATUSES
            if response.status_code == 409:
                try:
                    retryable = response.json().get("code") in {"40P01", "40001"}
                except ValueError:
                    pass
            if not retryable:
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    raise requests.HTTPError(f"{exc}; body={response.text[:1000]}", response=response) from exc
                return response

            if attempt == 5:
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    raise requests.HTTPError(f"{exc}; body={response.text[:1000]}", response=response) from exc

            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(30.0, 2 ** (attempt - 1) + random.random())
            time.sleep(delay)
        except requests.RequestException as exc:
            if isinstance(exc, requests.HTTPError) and exc.response is not None and not retryable:
                raise
            last_error = exc
            if attempt == 5:
                raise
            time.sleep(min(30.0, 2 ** (attempt - 1) + random.random()))

    if last_error:
        raise last_error
    raise RuntimeError("request failed")


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def batched(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def synthetic_citytag_id(city_id: int, tripster_tag_id: int) -> int:
    return -((city_id * 100000) + tripster_tag_id)


class SupabaseRest:
    def __init__(self, url: str, service_role_key: str, timeout: float) -> None:
        self.base_url = f"{url.rstrip('/')}/rest/v1"
        self.timeout = timeout
        self.headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
        }

    def rpc(self, name: str, params: dict[str, Any]) -> Any:
        response = request_with_retries(
            "POST", f"{self.base_url}/rpc/{name}", headers=self.headers,
            json=params, timeout=self.timeout,
        )
        return response.json() if response.content else None

    def patch_ids(self, table: str, key: str, ids: list[int], values: dict[str, Any]) -> None:
        for batch in batched(sorted(set(ids)), DELETE_ID_CHUNK_SIZE):
            request_with_retries(
                "PATCH", f"{self.base_url}/{table}", headers=self.headers,
                params={key: f"in.({','.join(map(str, batch))})"},
                json=values, timeout=self.timeout,
            )

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

    def select_all_keyset(
        self,
        table: str,
        select: str,
        key_column: str,
        *,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        last_value: Any = None

        while True:
            params: dict[str, Any] = {
                "select": select,
                "limit": REST_PAGE_SIZE,
                "order": f"{key_column}.asc",
            }
            if filters:
                params.update(filters)
            if last_value is not None:
                params[key_column] = f"gt.{last_value}"

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
            last_value = page[-1][key_column]

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
        if not rows:
            return

        headers = {**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(tuple(sorted(row)), []).append(row)
        for group_key in sorted(groups):
            group = groups[group_key]
            conflict_keys = on_conflict.split(",")
            group = sorted(group, key=lambda row: tuple(row[key] for key in conflict_keys))
            for batch in batched(group, UPSERT_CHUNK_SIZE):
                request_with_retries(
                    "POST", f"{self.base_url}/{table}", headers=headers,
                    params={"on_conflict": on_conflict}, json=batch, timeout=self.timeout,
                )

    def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return

        headers = {**self.headers, "Prefer": "return=minimal"}
        for batch in batched(rows, INSERT_CHUNK_SIZE):
            request_with_retries(
                "POST",
                f"{self.base_url}/{table}",
                headers=headers,
                json=batch,
                timeout=self.timeout,
            )

    def select_existing_experience_ids(self, experience_ids: list[int]) -> set[int]:
        if not experience_ids:
            return set()

        existing_ids: set[int] = set()
        unique_ids = sorted(set(experience_ids))
        for batch in [
            unique_ids[index : index + DELETE_ID_CHUNK_SIZE]
            for index in range(0, len(unique_ids), DELETE_ID_CHUNK_SIZE)
        ]:
            response = request_with_retries(
                "GET",
                f"{self.base_url}/experiences",
                headers=self.headers,
                params={"select": "id", "id": f"in.({','.join(map(str, batch))})"},
                timeout=self.timeout,
            )
            page = response.json()
            if not isinstance(page, list):
                raise RuntimeError(f"Unexpected Supabase response for experiences: {page!r}")
            for row in page:
                row_id = as_int(row.get("id")) if isinstance(row, dict) else None
                if row_id is not None:
                    existing_ids.add(row_id)
        return existing_ids

    def delete_by_experience_ids(self, table: str, experience_ids: list[int]) -> None:
        if not experience_ids:
            return

        headers = {**self.headers, "Prefer": "return=minimal"}
        unique_ids = sorted(set(experience_ids))
        for batch in [
            unique_ids[index : index + DELETE_ID_CHUNK_SIZE]
            for index in range(0, len(unique_ids), DELETE_ID_CHUNK_SIZE)
        ]:
            request_with_retries(
                "DELETE",
                f"{self.base_url}/{table}",
                headers=headers,
                params={"experience_id": f"in.({','.join(map(str, batch))})"},
                timeout=self.timeout,
            )


def normalize_tripster_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        if isinstance(payload[0].get("results"), list):
            return payload[0]
    raise RuntimeError(f"Unexpected Tripster response shape: {type(payload).__name__}")


def pick_geo(item: dict[str, Any], key: str) -> dict[str, Any]:
    values = (item.get("geo") or {}).get(key) or []
    if not isinstance(values, list) or not values:
        return {}
    for value in values:
        if isinstance(value, dict) and value.get("is_main_city"):
            return value
    return values[0] if isinstance(values[0], dict) else {}


def map_experience(
    item: dict[str, Any],
    fallback_city_id: int,
    guide_ids: set[int],
    country_ids: set[int],
    city_ids: set[int],
) -> dict[str, Any] | None:
    experience_id = as_int(item.get("id"))
    if experience_id is None or not item.get("title"):
        return None

    price = item.get("price") or {}
    country = pick_geo(item, "country")
    city = pick_geo(item, "city")
    region = pick_geo(item, "region")
    guide = item.get("guide") or {}
    schedule = item.get("schedule")

    city_id = as_int(city.get("id")) or fallback_city_id
    country_id = as_int(country.get("id"))
    guide_id = as_int(guide.get("id"))
    detail_present = item.get("description") is not None

    if city_id not in city_ids:
        city_id = fallback_city_id if fallback_city_id in city_ids else None
    if country_id not in country_ids:
        country_id = None
    if guide_id not in guide_ids:
        guide_id = None

    row = {
        "id": experience_id,
        "status": item.get("status"),
        "title": item.get("title"),
        "tagline": item.get("tagline"),
        "annotation": item.get("annotation"),
        "url": item.get("url"),
        "type": item.get("type"),
        "format": item.get("format"),
        "grade": item.get("grade"),
        "movement_type": item.get("movement_type"),
        "schedule_type": item.get("schedule_type"),
        "exp_status": item.get("exp_status"),
        "is_instant_booking": item.get("is_instant_booking"),
        "is_under_sanctions": item.get("is_under_sanctions"),
        "is_new": item.get("is_new"),
        "show_promotion_badge": item.get("show_promotion_badge"),
        "guide_can_resume": item.get("guide_can_resume"),
        "has_non_refundable_part": item.get("has_non_refundable_part"),
        "child_friendly": item.get("child_friendly"),
        "is_paid": item.get("is_paid"),
        "is_paid_issuance": item.get("is_paid_issuance"),
        "max_persons": item.get("max_persons"),
        "duration": item.get("duration"),
        "rating": item.get("rating"),
        "visitors_count": item.get("visitors_count"),
        "review_count": item.get("review_count"),
        "price_value": price.get("value"),
        "price_currency": price.get("currency"),
        "price_currency_rate": price.get("currency_rate"),
        "price_per_group_value": (price.get("per_group") or {}).get("value"),
        "price_from": price.get("price_from"),
        "price_unit_string": price.get("unit_string"),
        "price_value_string": price.get("value_string"),
        "price_discount": price.get("discount"),
        "pause_finish_date": item.get("pause_finish_date"),
        "pause_reason": item.get("pause_reason"),
        "cancel_finish_date": item.get("cancel_finish_date"),
        "cancel_reason": item.get("cancel_reason"),
        "additional_info": item.get("additional_info"),
        "comfort_level_info": item.get("comfort_level_info"),
        "price_included_description": item.get("price_included_description"),
        "price_not_included_description": item.get("price_not_included_description"),
        "geo_access_level": item.get("geo_access_level"),
        "country_id": country_id,
        "city_id": city_id,
        "region_id": as_int(region.get("id")),
        "region_slug": region.get("slug"),
        "region_name": region.get("name"),
        "guide_id": guide_id,
        "legal_exp_type": item.get("legal_exp_type"),
        "accommodation": item.get("accommodation"),
        "children_allowed_age": item.get("children_allowed_age"),
        "pricing_model": item.get("pricing_model"),
        "minimal_price_info": item.get("minimal_price_info"),
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
        "labels": item.get("labels"),
        "options": item.get("options"),
        "integration": item.get("integration"),
        "timezone": item.get("timezone"),
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        # "raw_list_payload": item,
        # "raw_detail_payload": item if detail_present else None,
    }
    # List responses often omit the detailed fields. Preserve previously fetched
    # descriptions rather than replacing them with nulls during a full refresh.
    if not detail_present:
        for key in (
            "description_html", "meeting_point_text", "finish_point_text", "video_id",
            "photos_count", "cover_image_url", "cover_image_source", "schedule", "schedule_text",
            "languages", "legality_description", "labels", "options", "integration", "timezone",
        ):
            if row[key] is None:
                del row[key]
    return row


def map_tags(
    item: dict[str, Any],
    city_id: int,
    tag_citytag_map: dict[tuple[int, int], int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    experience_id = as_int(item.get("id"))
    tags = item.get("tags") or []
    if experience_id is None or not isinstance(tags, list):
        return [], []

    tag_rows: list[dict[str, Any]] = []
    link_rows: list[dict[str, Any]] = []
    used_tag_ids: set[int] = set()
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        tag_id = as_int(tag.get("id"))
        if tag_id is None or tag_id in used_tag_ids:
            continue
        used_tag_ids.add(tag_id)

        citytag_id = tag_citytag_map.get((city_id, tag_id))
        if citytag_id is None:
            citytag_id = synthetic_citytag_id(city_id, tag_id)
            tag_citytag_map[(city_id, tag_id)] = citytag_id
        is_synthetic = citytag_id < 0

        if is_synthetic:
            tag_rows.append(
                {
                    "id": tag_id,
                    "citytag_id": citytag_id,
                    "city_id": city_id,
                    "name": tag.get("name"),
                    "category": "Авто/SEO" if tag.get("is_auto") or tag.get("is_seo_only") else "Из экскурсий",
                    "is_common": tag.get("is_common"),
                    "is_auto": tag.get("is_auto"),
                    "is_seo_only": tag.get("is_seo_only"),
                    # "raw_payload": tag,
                }
            )
        link_rows.append({"experience_id": experience_id, "citytag_id": citytag_id})
    return tag_rows, link_rows


def map_photos(item: dict[str, Any]) -> list[dict[str, Any]]:
    experience_id = as_int(item.get("id"))
    photos = item.get("photos") or []
    if experience_id is None or not isinstance(photos, list):
        return []

    rows: list[dict[str, Any]] = []
    for position, photo in enumerate(photos[:MAX_PHOTOS_PER_EXPERIENCE]):
        if not isinstance(photo, dict):
            continue
        rows.append(
            {
                "experience_id": experience_id,
                "position": position,
                "type": photo.get("type"),
                "medium_url": photo.get("medium"),
                "thumbnail_url": photo.get("thumbnail"),
                # "thumbnail_s_url": photo.get("thumbnail_s"),
                # "thumbnail_m_url": photo.get("thumbnail_m"),
                # "thumbnail_l_url": photo.get("thumbnail_l"),
                # "thumbnail_xl_url": photo.get("thumbnail_xl"),
                # "raw_payload": photo,
            }
        )
    return rows


def fetch_tripster_page(
    token: str,
    city_slug: str,
    next_url: str | None,
    limit: int,
    timeout: float,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "ekskursii-bd-import/1.0",
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


def confirm_tripster_snapshot(
    token: str, slug: str, limit: int, timeout: float, expected_count: int, expected_ids: set[int],
) -> None:
    """Tripster's count can include filtered rows. Confirm every ID twice before accepting a discrepancy."""
    if not expected_ids:
        raise RuntimeError(f"{slug}: nonzero API count with an empty response; refresh aborted")
    observed: set[int] = set()
    visited: set[str] = set()
    next_url: str | None = None
    while True:
        page = fetch_tripster_page(token, slug, next_url, limit, timeout)
        if as_int(page.get("count")) != expected_count:
            raise RuntimeError(f"{slug}: API count changed during confirmation; retry refresh")
        for item in page["results"]:
            item_id = as_int(item.get("id")) if isinstance(item, dict) else None
            if item_id is None:
                raise RuntimeError(f"{slug}: invalid experience during confirmation")
            observed.add(item_id)
        next_url = page.get("next")
        if not next_url:
            break
        if next_url in visited:
            raise RuntimeError(f"{slug}: pagination loop during confirmation")
        visited.add(next_url)
    if observed != expected_ids:
        raise RuntimeError(f"{slug}: incomplete/unstable snapshot; ID sets differ between full reads")
    log(f"{slug}: API count={expected_count}, actual={len(observed)}; confirmed identical IDs in two full reads")


def import_city(
    db: SupabaseRest,
    city: dict[str, Any],
    token: str,
    guide_ids: set[int],
    country_ids: set[int],
    city_ids: set[int],
    limit: int,
    timeout: float,
    dry_run: bool,
    photos_only: bool,
) -> tuple[int, int, int, int, set[int]]:
    slug = str(city["slug"])
    fallback_city_id = int(city["id"])
    next_url: str | None = None
    pages = imported_count = tag_count = photo_count = 0
    all_experience_rows: list[dict[str, Any]] = []
    all_tag_rows_by_citytag_id: dict[int, dict[str, Any]] = {}
    all_tag_link_rows: list[dict[str, Any]] = []
    all_photo_rows: list[dict[str, Any]] = []
    all_refresh_relation_ids: list[int] = []
    seen_ids: set[int] = set()
    visited_pages: set[str] = set()
    expected_count: int | None = None
    fetched_count = 0
    tag_citytag_map: dict[tuple[int, int], int] = {}
    canonical_tags: dict[int, dict[str, Any]] = {}
    canonical_citytags: dict[int, dict[str, Any]] = {}

    if not photos_only:
        tag_next: str | None = None
        tag_pages: set[str] = set()
        expected_tags: int | None = None
        fetched_tags = 0
        while True:
            tag_page = fetch_citytags_page(token, fallback_city_id, tag_next, timeout)
            fetched_tags += len(tag_page["results"])
            if expected_tags is None:
                expected_tags = as_int(tag_page.get("count"))
                if expected_tags is None or expected_tags < 0:
                    raise RuntimeError(f"{slug}: citytags response has no valid count")
            for item in tag_page["results"]:
                mapped = map_citytag(item, fallback_city_id)
                if mapped is None:
                    raise RuntimeError(f"{slug}: invalid citytag; refresh aborted")
                tag, citytag = mapped
                canonical_tags[int(tag["id"])] = tag
                canonical_citytags[int(citytag["citytag_id"])] = citytag
                tag_citytag_map[(fallback_city_id, int(tag["id"]))] = int(citytag["citytag_id"])
            tag_next = tag_page.get("next")
            if not tag_next:
                break
            if tag_next in tag_pages:
                raise RuntimeError(f"{slug}: citytags pagination loop")
            tag_pages.add(tag_next)
        if expected_tags is not None and fetched_tags != expected_tags:
            raise RuntimeError(f"{slug}: incomplete citytags snapshot ({fetched_tags}/{expected_tags})")

    while True:
        page = fetch_tripster_page(token, slug, next_url, limit, timeout)
        results = page.get("results")
        if not isinstance(results, list):
            raise RuntimeError(f"Unexpected results value for {slug}: {type(results).__name__}")

        if expected_count is None:
            expected_count = as_int(page.get("count"))
            if expected_count is None or expected_count < 0:
                raise RuntimeError(f"{slug}: experiences response has no valid count")
        fetched_count += len(results)
        fresh = []
        for item in results:
            item_id = as_int(item.get("id")) if isinstance(item, dict) else None
            if item_id is None:
                raise RuntimeError(f"{slug}: invalid experience; refresh aborted")
            if item_id not in seen_ids:
                fresh.append(item)
                seen_ids.add(item_id)

        experience_rows: list[dict[str, Any]] = []
        tag_rows_by_citytag_id: dict[int, dict[str, Any]] = {}
        tag_link_rows: list[dict[str, Any]] = []
        photo_rows: list[dict[str, Any]] = []
        refresh_relation_ids: list[int] = []

        for item in fresh:
            if photos_only:
                experience_id = as_int(item.get("id"))
                if experience_id is None:
                    continue
                refresh_relation_ids.append(experience_id)
                photo_rows.extend(map_photos(item))
                continue

            experience = map_experience(item, fallback_city_id, guide_ids, country_ids, city_ids)
            if experience is None:
                raise RuntimeError(f"{slug}: invalid experience {item.get('id')}; refresh aborted")

            experience_rows.append(experience)
            experience_id = int(experience["id"])
            # The list is queried for this city; citytag IDs belong to that scope,
            # even when an excursion's main geographical city is different.
            city_id = fallback_city_id
            refresh_relation_ids.append(experience_id)
            if not isinstance(item.get("tags"), list) or any(
                not isinstance(tag, dict) or as_int(tag.get("id")) is None for tag in item["tags"]
            ):
                raise RuntimeError(f"{slug}: invalid/missing tags for {experience_id}; refresh aborted")
            tag_rows, links = map_tags(item, city_id, tag_citytag_map)
            for tag_row in tag_rows:
                tag_rows_by_citytag_id[int(tag_row["citytag_id"])] = tag_row
                # Tags absent from the citytags endpoint still need a canonical
                # record so the new catalog can resolve the excursion's links.
                tag_id = int(tag_row["id"])
                if tag_id not in canonical_tags:
                    canonical_tags[tag_id] = {
                        "id": tag_id, "name": tag_row["name"],
                        **{key: tag_row[key] for key in ("is_common", "is_auto", "is_seo_only")
                           if tag_row.get(key) is not None},
                    }
                canonical_citytags[int(tag_row["citytag_id"])] = {
                    "citytag_id": tag_row["citytag_id"], "tag_id": tag_id,
                    "city_id": city_id, "name": tag_row["name"],
                    "is_hidden": False, "experience_count": 1,
                }
            tag_link_rows.extend(links)
            photo_rows.extend(map_photos(item))

        all_experience_rows.extend(experience_rows)
        all_tag_rows_by_citytag_id.update(tag_rows_by_citytag_id)
        all_tag_link_rows.extend(tag_link_rows)
        all_photo_rows.extend(photo_rows)
        all_refresh_relation_ids.extend(refresh_relation_ids)

        pages += 1
        imported_count += len(refresh_relation_ids) if photos_only else len(experience_rows)
        tag_count += len(tag_link_rows)
        photo_count += len(photo_rows)

        next_url = page.get("next")
        if next_url:
            if next_url in visited_pages:
                raise RuntimeError(f"{slug}: experiences pagination loop")
            visited_pages.add(next_url)
        log(
            f"{slug}: page={pages}, fetched={len(results)}, imported={len(experience_rows)}, "
            f"tags={len(tag_link_rows)}, photos={len(photo_rows)}, next={'yes' if next_url else 'no'}"
        )
        if not next_url:
            if expected_count is not None and (fetched_count != expected_count or len(seen_ids) != expected_count):
                confirm_tripster_snapshot(token, slug, limit, timeout, expected_count, seen_ids)
            if not dry_run and not photos_only:
                with write_lock:
                    db.upsert("tripster_tags", list(canonical_tags.values()), "id")
                    db.upsert("tripster_city_tags", list(canonical_citytags.values()), "citytag_id")
                    old_citytags = db.select_all(
                        "tripster_city_tags", "citytag_id", filters={"city_id": f"eq.{fallback_city_id}"},
                        order="citytag_id.asc",
                    )
                    stale_ids = [int(row["citytag_id"]) for row in old_citytags
                                 if int(row["citytag_id"]) not in canonical_citytags]
                    db.patch_ids("tripster_city_tags", "citytag_id", stale_ids,
                                 {"is_hidden": True, "experience_count": 0, "excursions_count": 0, "tours_count": 0})
                    # Legacy FK compatibility: experience_tags still references tags.
                    for ct in canonical_citytags.values():
                        tag = canonical_tags[int(ct["tag_id"])]
                        bridge = {key: value for key, value in ct.items() if key != "tag_id"}
                        bridge.update({"id": ct["tag_id"], **{
                            key: bool(tag.get(key)) for key in ("is_common", "is_auto", "is_seo_only", "is_attraction")
                        }})
                        all_tag_rows_by_citytag_id[int(ct["citytag_id"])] = bridge
                    for bridge_batch in batched(list(all_tag_rows_by_citytag_id.values()), UPSERT_CHUNK_SIZE):
                        db.rpc("upsert_tripster_tag_bridge_v2", {"p_rows": bridge_batch})
                    db.upsert("experiences", all_experience_rows, "id")
                    for batch in batched(all_refresh_relation_ids, UPSERT_CHUNK_SIZE):
                        ids = set(batch)
                        db.rpc("replace_tripster_relations_v2", {
                            "p_experience_ids": batch,
                            "p_tag_links": [row for row in all_tag_link_rows if row["experience_id"] in ids],
                            "p_photos": [row for row in all_photo_rows if row["experience_id"] in ids],
                        })
            elif not dry_run:
                with write_lock:
                    existing_ids = db.select_existing_experience_ids(all_refresh_relation_ids)
                    missing_count = len(set(all_refresh_relation_ids) - existing_ids)
                    if missing_count:
                        log(f"{slug}: skipped photos for {missing_count} missing experiences")
                    all_refresh_relation_ids = [
                        experience_id for experience_id in all_refresh_relation_ids if experience_id in existing_ids
                    ]
                    all_photo_rows = [
                        row for row in all_photo_rows if as_int(row.get("experience_id")) in existing_ids
                    ]
                    db.delete_by_experience_ids("experience_photos", all_refresh_relation_ids)
                    db.insert("experience_photos", all_photo_rows)
            return pages, imported_count, tag_count, photo_count, seen_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Tripster experiences into Supabase.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--city-slug", action="append", help="One slug or comma-separated slug list.")
    parser.add_argument("--max-cities", type=int)
    parser.add_argument(
        "--incomplete-only",
        action="store_true",
        help="Import only cities where local experiences count is lower than cities.experience_count.",
    )
    parser.add_argument(
        "--photos-only",
        action="store_true",
        help="Refresh only experience_photos from Tripster list payload.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume the unfinished full refresh from its checkpoint.")
    parser.add_argument("--checkpoint", default=str(Path(__file__).with_name("tripster_refresh_checkpoint.json")))
    return parser.parse_args()


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()
    if args.limit < 1 or args.workers < 1 or (args.max_cities is not None and args.max_cities < 1):
        raise SystemExit("--limit, --workers and --max-cities must be positive")
    started_at = datetime.now(timezone.utc).isoformat()

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

    log("Loading cities, guides and countries from Supabase...")
    cities = db.select_all(
        "cities",
        "id,slug,name_ru,experience_count",
        filters={"slug": "not.is.null"},
        order="id.asc",
    )
    if args.city_slug:
        wanted = {slug.strip() for value in args.city_slug for slug in value.split(",") if slug.strip()}
        missing = wanted - {city["slug"] for city in cities}
        if missing:
            raise SystemExit(f"Unknown city slug(s): {', '.join(sorted(missing))}")
        cities = [city for city in cities if city.get("slug") in wanted]
    if args.incomplete_only:
        imported_by_city: dict[int, int] = {}
        for experience in db.select_all_keyset("experiences", "id,city_id", "id"):
            city_id = as_int(experience.get("city_id"))
            if city_id is not None:
                imported_by_city[city_id] = imported_by_city.get(city_id, 0) + 1
        cities = [
            city
            for city in cities
            if imported_by_city.get(int(city["id"]), 0) < int(city.get("experience_count") or 0)
        ]
    if args.max_cities:
        cities = cities[: args.max_cities]
    if not cities:
        raise SystemExit("No cities selected")

    checkpoint_path = Path(args.checkpoint) if args.checkpoint and not args.photos_only and not args.incomplete_only else None
    checkpoint: dict[str, Any] = {
        "started_at": started_at, "city_ids": sorted(int(city["id"]) for city in cities), "completed": {},
    }
    if args.resume:
        if checkpoint_path is None or not checkpoint_path.exists():
            raise SystemExit("No full-refresh checkpoint available for --resume")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("finished_at") or checkpoint.get("city_ids") != sorted(int(city["id"]) for city in cities):
            raise SystemExit("Checkpoint is already finished or belongs to a different city scope")
        started_at = checkpoint["started_at"]

    def save_checkpoint() -> None:
        if checkpoint_path is not None and not args.dry_run:
            temporary_path = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
            temporary_path.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
            temporary_path.replace(checkpoint_path)

    save_checkpoint()

    guide_ids: set[int] = set()
    country_ids: set[int] = set()
    city_ids: set[int] = set()
    if not args.photos_only:
        guide_ids = {int(row["id"]) for row in db.select_all("guides", "id", order="id.asc") if row.get("id") is not None}
        country_ids = {int(row["id"]) for row in db.select_all("countries", "id", order="id.asc") if row.get("id") is not None}
        city_ids = {int(row["id"]) for row in db.select_all("cities", "id", order="id.asc") if row.get("id") is not None}
        if not args.dry_run:
            # Fail before writing any data when the required SQL helpers are absent.
            db.rpc("replace_tripster_relations_v2", {"p_experience_ids": [], "p_tag_links": [], "p_photos": []})
            db.rpc("upsert_tripster_tag_bridge_v2", {"p_rows": []})
            db.rpc("sync_experience_catalog_links_v2", {"p_experience_ids": []})
            db.rpc("finalize_tripster_catalog_refresh_v2", {
                "p_city_ids": [], "p_seen_ids": [], "p_started_at": started_at,
            })

    mode = "DRY RUN" if args.dry_run else "IMPORT"
    target = "photos only" if args.photos_only else "full import"
    log(f"{mode}: {target}, cities={len(cities)}, workers={args.workers}, limit={args.limit}")

    total_pages = total_imported = total_tags = total_photos = total_errors = completed_cities = 0
    total_cities = len(cities)
    all_seen_ids: set[int] = set()
    for saved in checkpoint["completed"].values():
        all_seen_ids.update(saved["seen_ids"])
        total_pages += saved["pages"]
        total_imported += saved["imported"]
        total_tags += saved["tags"]
        total_photos += saved["photos"]
        completed_cities += 1
    pending_cities = [city for city in cities if str(city["id"]) not in checkpoint["completed"]]
    log(f"Refresh started_at={started_at}; already completed={completed_cities}; remaining={len(pending_cities)}")
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                import_city,
                db,
                city,
                tripster_token,
                guide_ids,
                country_ids,
                city_ids,
                args.limit,
                args.timeout,
                args.dry_run,
                args.photos_only,
            ): city
            for city in pending_cities
        }

        for future in as_completed(futures):
            city = futures[future]
            try:
                pages, imported, tags, photos, seen_ids = future.result()
            except Exception as exc:
                total_errors += 1
                completed_cities += 1
                log(
                    f"{city.get('slug')}: ERROR: {exc} "
                    f"(progress={completed_cities}/{total_cities}, remaining={total_cities - completed_cities}, "
                    f"errors={total_errors})"
                )
                continue
            completed_cities += 1
            total_pages += pages
            total_imported += imported
            total_tags += tags
            total_photos += photos
            all_seen_ids.update(seen_ids)
            checkpoint["completed"][str(city["id"])] = {
                "pages": pages, "imported": imported, "tags": tags, "photos": photos, "seen_ids": sorted(seen_ids),
            }
            save_checkpoint()
            log(
                f"{city.get('slug')}: done "
                f"(progress={completed_cities}/{total_cities}, remaining={total_cities - completed_cities}, "
                f"errors={total_errors})"
            )

    if not args.photos_only:
        if args.dry_run:
            log(f"DRY RUN: would rebuild catalog links for {len(all_seen_ids)} experiences; no writes made")
        elif total_errors == 0:
            if not checkpoint.get("catalog_synced"):
                db.rpc("sync_tripster_local_catalog_v2", {})
                checkpoint["catalog_synced"] = True
                save_checkpoint()
                log("Local catalog metadata synchronized")
            ordered_ids = sorted(all_seen_ids)
            link_offset = int(checkpoint.get("catalog_link_offset", 0))
            for batch in batched(ordered_ids[link_offset:], UPSERT_CHUNK_SIZE):
                count = db.rpc("sync_experience_catalog_links_v2", {"p_experience_ids": batch})
                link_offset += len(batch)
                checkpoint["catalog_link_offset"] = link_offset
                save_checkpoint()
                log(f"Catalog links rebuilt: progress={link_offset}/{len(ordered_ids)}, links={count}")
            if not args.incomplete_only:
                inactive = int(checkpoint.get("inactive_count", 0))
                while True:
                    changed = db.rpc("finalize_tripster_catalog_refresh_v2", {
                        "p_city_ids": [int(city["id"]) for city in cities],
                        "p_seen_ids": ordered_ids, "p_started_at": started_at,
                    })
                    if not isinstance(changed, int) or changed < 0:
                        raise RuntimeError(f"Unexpected finalization result: {changed!r}")
                    inactive += changed
                    checkpoint["inactive_count"] = inactive
                    save_checkpoint()
                    if changed == 0:
                        break
                    log(f"Missing experiences cleanup: batch={changed}, total_inactive={inactive}")
                log(f"Missing experiences marked inactive: {inactive}")
                checkpoint["finished_at"] = datetime.now(timezone.utc).isoformat()
                save_checkpoint()
            else:
                log("Incomplete-only import: missing experiences are not deactivated")
        else:
            log("Refresh incomplete: catalog finalization and deactivation skipped; rerun the import")

    log(
        f"Done: pages={total_pages}, imported={total_imported}, "
        f"tags={total_tags}, photos={total_photos}, city_errors={total_errors}"
    )
    if total_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
