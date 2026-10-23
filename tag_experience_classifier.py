from __future__ import annotations

import argparse
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests


REST_PAGE_SIZE = 1000
DEFAULT_API_BASE = "https://api.aigate.shop/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"
DEFAULT_BATCH_SIZE = 3
DEFAULT_THINKING_BUDGET_TOKENS = 4096
PROMPT_CACHE_MIN_WORDS = 500
RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
print_lock = threading.Lock()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log(message: str) -> None:
    with print_lock:
        print(message, flush=True)


def request_with_retries(
    method: str,
    url: str,
    *,
    timeout: float,
    max_attempts: int,
    **kwargs: Any,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code not in RETRYABLE_STATUSES:
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    raise requests.HTTPError(f"{exc}; body={response.text[:1000]}", response=response) from exc
                return response

            if attempt == max_attempts:
                response.raise_for_status()
        except requests.RequestException as exc:
            last_error = exc
            if attempt == max_attempts:
                raise

        retry_after = response.headers.get("Retry-After") if "response" in locals() else None
        delay = float(retry_after) if retry_after else min(30.0, 2 ** (attempt - 1) + random.random())
        time.sleep(delay)

    if last_error:
        raise last_error
    raise RuntimeError("request failed")


class SupabaseRest:
    def __init__(self, url: str, key: str, timeout: float, max_retries: int) -> None:
        self.base_url = f"{url.rstrip('/')}/rest/v1"
        self.timeout = timeout
        self.max_retries = max_retries
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
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
        rows: list[dict[str, Any]] = []
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
                max_attempts=self.max_retries,
            )
            page = response.json()
            if not isinstance(page, list):
                raise RuntimeError(f"Unexpected Supabase response for {table}: {page!r}")
            rows.extend(page)
            if len(page) < REST_PAGE_SIZE:
                return rows
            offset += REST_PAGE_SIZE

    def select_first(
        self,
        table: str,
        select: str,
        *,
        filters: dict[str, str] | None = None,
        order: str | None = None,
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {"select": select, "limit": 1}
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
            max_attempts=self.max_retries,
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected Supabase response for {table}: {rows!r}")
        return rows[0] if rows else None

    def rpc(self, function: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        response = request_with_retries(
            "POST",
            f"{self.base_url}/rpc/{function}",
            headers=self.headers,
            json=params,
            timeout=self.timeout,
            max_attempts=self.max_retries,
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected Supabase RPC response for {function}: {rows!r}")
        return rows

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
        if not rows:
            return
        request_with_retries(
            "POST",
            f"{self.base_url}/{table}",
            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            params={"on_conflict": on_conflict},
            json=rows,
            timeout=self.timeout,
            max_attempts=self.max_retries,
        )

    def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        request_with_retries(
            "POST",
            f"{self.base_url}/{table}",
            headers={**self.headers, "Prefer": "return=minimal"},
            json=rows,
            timeout=self.timeout,
            max_attempts=self.max_retries,
        )

    def delete_by_experience_ids(self, table: str, experience_ids: list[int]) -> None:
        if not experience_ids:
            return
        request_with_retries(
            "DELETE",
            f"{self.base_url}/{table}",
            headers={**self.headers, "Prefer": "return=minimal"},
            params={"experience_id": f"in.({','.join(str(item) for item in sorted(set(experience_ids)))})"},
            timeout=self.timeout,
            max_attempts=self.max_retries,
        )


def chunked_values(items: list[int], size: int = 100) -> list[list[int]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def supabase_from_env(timeout: float, max_retries: int) -> SupabaseRest:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env")
    return SupabaseRest(url, key, timeout, max_retries)


def unique_clean(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value or "").strip())
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def split_csv_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return unique_clean([str(item) for item in value])
    return unique_clean([part.strip() for part in str(value or "").split(",")])


def strip_code_fence(content: str) -> str:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def parse_llm_json(content: str) -> dict[str, Any]:
    value = strip_code_fence(content)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", value):
            try:
                parsed, _ = decoder.raw_decode(value[match.start() :])
            except json.JSONDecodeError:
                continue
            break
        else:
            raise

    if not isinstance(parsed, dict):
        raise RuntimeError(f"LLM JSON must be an object: {parsed!r}")
    return parsed


def pad_for_prompt_cache(text: str) -> str:
    word_count = len(re.findall(r"\S+", text))
    if word_count >= PROMPT_CACHE_MIN_WORDS:
        return text
    filler = " ".join(f"cachepad{i:04d}" for i in range(PROMPT_CACHE_MIN_WORDS - word_count))
    return f"{text}\n{filler}"


def fetch_allowed_catalog(db: SupabaseRest) -> tuple[list[str], dict[str, int]]:
    rows = db.select_all(
        "tag_category_catalog",
        "id,sub_slug",
        filters={"is_common": "eq.true"},
        order="sub_slug.asc",
    )
    sub_slugs = unique_clean([row.get("sub_slug", "") for row in rows])
    catalog_ids_by_sub_slug: dict[str, int] = {}
    for row in rows:
        sub_slug = str(row.get("sub_slug") or "").strip()
        if sub_slug and sub_slug not in catalog_ids_by_sub_slug:
            catalog_ids_by_sub_slug[sub_slug] = int(row["id"])
    return sub_slugs, catalog_ids_by_sub_slug


def fetch_processed_ids(db: SupabaseRest) -> set[int]:
    rows = db.select_all("tag_experience_table", "experience_id")
    return {int(row["experience_id"]) for row in rows if row.get("experience_id") is not None}


def attach_tripster_common_tags(db: SupabaseRest, experiences: list[dict[str, Any]]) -> None:
    experience_ids = [int(item["id"]) for item in experiences]
    if not experience_ids:
        return

    experience_by_citytag_id: dict[int, set[int]] = {}
    all_citytag_ids_by_experience: dict[int, set[int]] = {experience_id: set() for experience_id in experience_ids}
    for batch in chunked_values(experience_ids):
        rows = db.select_all(
            "experience_tags",
            "experience_id,citytag_id",
            filters={"experience_id": f"in.({','.join(str(item) for item in batch)})"},
        )
        for row in rows:
            citytag_id = int(row["citytag_id"])
            experience_id = int(row["experience_id"])
            experience_by_citytag_id.setdefault(citytag_id, set()).add(experience_id)
            all_citytag_ids_by_experience[experience_id].add(citytag_id)

    if not experience_by_citytag_id:
        for experience in experiences:
            experience["tripster_common_tags"] = []
            experience["tripster_all_tags_count"] = 0
        return

    citytag_to_tag_id: dict[int, int] = {}
    citytag_ids = list(experience_by_citytag_id)
    for batch in chunked_values(citytag_ids):
        rows = db.select_all(
            "tripster_city_tags",
            "citytag_id,tag_id",
            filters={"citytag_id": f"in.({','.join(str(item) for item in batch)})"},
        )
        for row in rows:
            citytag_to_tag_id[int(row["citytag_id"])] = int(row["tag_id"])

    common_tags_by_id: dict[int, dict[str, str]] = {}
    tag_ids = list(set(citytag_to_tag_id.values()))
    for batch in chunked_values(tag_ids):
        rows = db.select_all(
            "tripster_tags",
            "id,name,slug",
            filters={
                "id": f"in.({','.join(str(item) for item in batch)})",
                "is_common": "eq.true",
            },
        )
        for row in rows:
            common_tags_by_id[int(row["id"])] = {
                "name": str(row.get("name") or ""),
                "slug": str(row.get("slug") or ""),
            }

    tags_by_experience_id: dict[int, list[dict[str, str]]] = {experience_id: [] for experience_id in experience_ids}
    for citytag_id, linked_experience_ids in experience_by_citytag_id.items():
        common_tag = common_tags_by_id.get(citytag_to_tag_id.get(citytag_id, 0))
        if not common_tag:
            continue
        for experience_id in linked_experience_ids:
            tags_by_experience_id[experience_id].append(common_tag)

    for experience in experiences:
        seen: set[tuple[str, str]] = set()
        common_tags: list[dict[str, str]] = []
        for tag in tags_by_experience_id[int(experience["id"])]:
            key = (tag["name"], tag["slug"])
            if key not in seen:
                seen.add(key)
                common_tags.append(tag)
        experience["tripster_common_tags"] = common_tags
        experience["tripster_all_tags_count"] = len(all_citytag_ids_by_experience[int(experience["id"])])


def fetch_experiences(
    db: SupabaseRest,
    *,
    limit: int,
    experience_ids: list[int] | None,
    include_processed: bool,
) -> list[dict[str, Any]]:
    processed_ids = set() if include_processed else fetch_processed_ids(db)

    if experience_ids:
        rows = db.select_all(
            "experiences",
            "id,title,annotation",
            filters={"id": f"in.({','.join(str(item) for item in experience_ids)})"},
            order="id.asc",
        )
        return [row for row in rows if int(row["id"]) not in processed_ids][:limit]

    selected: list[dict[str, Any]] = []
    last_id = 0
    while len(selected) < limit:
        rows = db.select_all(
            "experiences",
            "id,title,annotation",
            filters={"id": f"gt.{last_id}", "title": "not.is.null"},
            order="id.asc",
        )
        if not rows:
            return selected
        for row in rows:
            last_id = int(row["id"])
            if int(row["id"]) in processed_ids:
                continue
            selected.append(row)
            if len(selected) >= limit:
                break
    return selected


def build_messages(
    experiences: list[dict[str, Any]],
    allowed_sub_slugs: list[str],
    *,
    use_prompt_cache: bool,
    allow_new_and_places: bool,
    include_tripster_common_tags: bool = True,
) -> list[dict[str, Any]]:
    required_result: dict[str, Any] = {
        "experience_id": 123,
        "tags": "existing-sub-slug-1,existing-sub-slug-2",
    }
    if allow_new_and_places:
        required_result.update(
            {
                "new_tags": "missing category tag 1,missing category tag 2",
                "places_tags": "Куршская коса,Светлогорск,Балтийское море",
            }
        )

    payload_experiences: list[dict[str, Any]] = []
    for item in experiences:
        payload_item: dict[str, Any] = {
            "experience_id": item["id"],
            "title": item.get("title") or "",
            "tagline": item.get("tagline") or "",
            "annotation": item.get("annotation") or "",
        }
        if include_tripster_common_tags:
            payload_item["tripster_common_tags"] = item.get("tripster_common_tags") or []
        payload_experiences.append(payload_item)
    payload = {
        "experiences": payload_experiences,
        "required_output_json": {
            "results": [required_result]
        },
    }
    instructions = (
        "Ты классифицируешь экскурсии по закрытому списку категорий. "
        "Для каждой экскурсии подбери максимально релевантные tags только из allowed_slugs. "
        "Не ограничивай количество tags: верни все максимально релевантные категории. "
        "Используй только title, tagline и annotation, не выдумывай факты. "
    )
    if include_tripster_common_tags:
        instructions += (
            "tripster_common_tags содержат common-теги этой экскурсии из старого Tripster-каталога: используй их как дополнительную подсказку, "
            "но выбирай итоговые tags только если они подтверждаются title или annotation. "
        )
    if allow_new_and_places:
        instructions += (
            "В поле new_tags добавь через запятую релевантные НЕгеографические теги/категории, которых нет в allowed_slugs, но их стоит создать. "
            "В поле places_tags добавь через запятую конкретные места, города, районы, природные объекты и достопримечательности из экскурсии. "
            "Примеры places_tags: Куршская коса, Светлогорск, Балтийское море, Танцующий лес, высота Эфа. "
            "Географические места не пиши в new_tags, только в places_tags. "
            "Не возвращай полностью пустую разметку: если tags не подходят, обязательно предложи хотя бы один new_tags или places_tags по title/annotation. "
        )
    else:
        instructions += (
            "Не придумывай новые теги, места или категории. "
            "Верни только tags; если подходящей категории нет, верни пустую строку. "
        )
    instructions += (
        "Ответ: один JSON без markdown и пояснений, по одному result на каждый experience_id."
    )
    allowed_categories = f"allowed_slugs={','.join(allowed_sub_slugs)}"

    if use_prompt_cache:
        cache_hint = (
            "STATIC_ALLOWED_CATEGORY_SLUGS. "
            "Use only these exact slugs in tags. "
            "Default mode is closed-set: return only allowed tags. "
            "Do not invent categories unless extended mode is explicitly requested."
        )
        cached_categories = pad_for_prompt_cache(f"{cache_hint}\n{allowed_categories}")
        system_content: str | list[dict[str, Any]] = [
            {"type": "text", "text": instructions},
            {
                "type": "text",
                "text": cached_categories,
                "cache_control": {"type": "ephemeral"},
            },
        ]
    else:
        system_content = f"{instructions} {allowed_categories}"

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def call_llm(
    *,
    messages: list[dict[str, Any]],
    api_key: str,
    api_base: str,
    model: str,
    timeout: float,
    max_retries: int,
    use_reasoning: bool,
    force_openai_reasoning: bool = False,
) -> dict[str, Any]:
    request_payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
    }
    if use_reasoning:
        if model.startswith("anthropic/") and not force_openai_reasoning:
            request_payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": DEFAULT_THINKING_BUDGET_TOKENS,
            }
        else:
            request_payload["reasoning"] = {
                "enabled": True,
                "effort": "high",
            }

    response = request_with_retries(
        "POST",
        f"{api_base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=request_payload,
        timeout=timeout,
        max_attempts=max_retries,
    )
    payload = response.json()
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices or not isinstance(choices, list):
        raise RuntimeError(f"Unexpected LLM response: {payload!r}")
    content = choices[0].get("message", {}).get("content")
    if not content:
        raise RuntimeError(f"LLM returned empty content: {payload!r}")
    result = parse_llm_json(str(content))
    if isinstance(result, dict):
        result["_usage"] = payload.get("usage")
    return result


def result_rows(
    proposal: dict[str, Any],
    experiences_by_id: dict[int, dict[str, Any]],
    allowed_sub_slugs: set[str],
    *,
    allow_new_and_places: bool,
) -> list[dict[str, Any]]:
    results = proposal.get("results")
    if results is None and "experience_id" in proposal and "tags" in proposal:
        results = [proposal]
    if not isinstance(results, list):
        raise RuntimeError(f"LLM JSON must contain results array: {proposal!r}")

    rows: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    experience_ids = set(experiences_by_id)
    for item in results:
        if not isinstance(item, dict):
            continue
        experience_id = int(item.get("experience_id"))
        if experience_id not in experience_ids:
            raise RuntimeError(f"LLM returned unexpected experience_id={experience_id}")

        tags = [tag for tag in split_csv_tags(item.get("tags")) if tag in allowed_sub_slugs]
        if allow_new_and_places:
            rejected_tags = [tag for tag in split_csv_tags(item.get("tags")) if tag not in allowed_sub_slugs]
            new_tags = [tag for tag in split_csv_tags(item.get("new_tags")) if tag not in allowed_sub_slugs]
            new_tags = unique_clean([*new_tags, *rejected_tags])
            places_tags = unique_clean(split_csv_tags(item.get("places_tags")))
            if not tags and not new_tags and not places_tags:
                title = experiences_by_id[experience_id].get("title") or ""
                raise RuntimeError(f"LLM returned empty tags for experience_id={experience_id}, title={title!r}")
        else:
            new_tags = []
            places_tags = []

        rows.append(
            {
                "experience_id": experience_id,
                "title": experiences_by_id[experience_id].get("title") or "",
                "tags": ",".join(tags),
                "new_tags": ",".join(new_tags),
                "places_tags": ",".join(places_tags),
            }
        )
        seen_ids.add(experience_id)

    missing_ids = experience_ids - seen_ids
    if missing_ids:
        raise RuntimeError(f"LLM missed experience_id values: {sorted(missing_ids)}")
    return rows


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def tags_count(value: Any) -> int:
    return len(split_csv_tags(value))


def log_result_row(row: dict[str, Any], *, dry_run: bool) -> None:
    prefix = "dry-run" if dry_run else "saved"
    log(
        f"{prefix}: experience_id={row['experience_id']} "
        f"title={row['title']!r} tags={tags_count(row['tags'])} "
        f"new_tags={tags_count(row['new_tags'])} places_tags={tags_count(row['places_tags'])}"
    )


def log_llm_usage(usage: Any, batch: list[dict[str, Any]]) -> None:
    if not isinstance(usage, dict):
        log(f"llm: experience_ids={','.join(str(item['id']) for item in batch)} usage=missing")
        return

    details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    log(
        f"llm: experience_ids={','.join(str(item['id']) for item in batch)} "
        f"prompt_tokens={usage.get('prompt_tokens', '')} "
        f"completion_tokens={usage.get('completion_tokens', '')} "
        f"total_tokens={usage.get('total_tokens', '')} "
        f"cached_tokens={details.get('cached_tokens', 0)} "
        f"cached_creation_tokens={details.get('cached_creation_tokens', 0)} "
        f"reasoning_tokens={completion_details.get('reasoning_tokens', 0)} "
        f"cost_usd={usage.get('cost_usd', '')}"
    )


def link_rows_for_experience_tags(
    rows: list[dict[str, Any]],
    catalog_ids_by_sub_slug: dict[str, int],
) -> list[dict[str, int]]:
    link_rows: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for row in rows:
        experience_id = int(row["experience_id"])
        for sub_slug in split_csv_tags(row.get("tags")):
            catalog_id = catalog_ids_by_sub_slug.get(sub_slug)
            if catalog_id is None:
                continue
            key = (catalog_id, experience_id)
            if key in seen:
                continue
            seen.add(key)
            link_rows.append({"catalog_id": catalog_id, "experience_id": experience_id})
    return link_rows


def process_batch(
    *,
    db: SupabaseRest,
    batch: list[dict[str, Any]],
    allowed_sub_slugs: list[str],
    allowed_set: set[str],
    catalog_ids_by_sub_slug: dict[str, int],
    api_key: str,
    args: argparse.Namespace,
) -> int:
    messages = build_messages(
        batch,
        allowed_sub_slugs,
        use_prompt_cache=args.use_prompt_cache,
        allow_new_and_places=args.allow_new_and_places,
    )
    proposal = call_llm(
        messages=messages,
        api_key=api_key,
        api_base=args.api_base,
        model=args.model,
        timeout=args.timeout,
        max_retries=args.max_retries,
        use_reasoning=not args.no_reasoning,
    )
    usage = proposal.get("_usage") if isinstance(proposal, dict) else None
    log_llm_usage(usage, batch)
    rows = result_rows(
        proposal,
        {int(item["id"]): item for item in batch},
        allowed_set,
        allow_new_and_places=args.allow_new_and_places,
    )
    if args.dry_run:
        for row in rows:
            log_result_row(row, dry_run=True)
        return 0

    db.upsert("tag_experience_table", rows, "experience_id")
    experience_ids = [int(row["experience_id"]) for row in rows]
    db.delete_by_experience_ids("experience_tags_new", experience_ids)
    link_rows = link_rows_for_experience_tags(rows, catalog_ids_by_sub_slug)
    db.insert("experience_tags_new", link_rows)
    for row in rows:
        log_result_row(row, dry_run=False)
    log(f"links: experiences={len(experience_ids)} experience_tags_new={len(link_rows)}")
    return len(rows)


def parse_int_list(values: list[str] | None) -> list[int] | None:
    if not values:
        return None
    result: list[int] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                result.append(int(part))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify experiences into tag_category_catalog sub_slug tags.")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_SIZE, help="Max experiences to process.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="LLM batch size, default 3.")
    parser.add_argument("--workers", type=int, default=1, help="Parallel LLM batches to process, e.g. 10.")
    parser.add_argument("--experience-id", action="append", help="One id or comma-separated ids to process.")
    parser.add_argument(
        "--include-processed",
        action="store_true",
        help="Allow overwriting rows already in tag_experience_table. By default existing rows are skipped.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Supabase.")
    parser.add_argument("--use-prompt-cache", action="store_true", help="Use provider prompt caching for the category list.")
    parser.add_argument("--no-reasoning", action="store_true", help="Disable LLM reasoning. Default: enabled with high effort.")
    parser.add_argument(
        "--allow-new-and-places",
        action="store_true",
        help="Allow LLM to return new_tags and places_tags. Default is closed-set tags only.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=os.environ.get("LLM_API_BASE") or DEFAULT_API_BASE)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()
    if args.batch_size < 1:
        raise RuntimeError("--batch-size must be >= 1")
    if args.workers < 1:
        raise RuntimeError("--workers must be >= 1")
    if args.batch_size != DEFAULT_BATCH_SIZE:
        log(f"batch_size={args.batch_size}; requested default batch is {DEFAULT_BATCH_SIZE}")

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("Set LLM_API_KEY in .env")

    db = supabase_from_env(args.timeout, args.max_retries)
    allowed_sub_slugs, catalog_ids_by_sub_slug = fetch_allowed_catalog(db)
    log(f"allowed_sub_slugs={','.join(allowed_sub_slugs)}")

    experience_ids = parse_int_list(args.experience_id)
    limit = len(experience_ids) if experience_ids else args.limit
    experiences = fetch_experiences(
        db,
        limit=limit,
        experience_ids=experience_ids,
        include_processed=args.include_processed,
    )
    if not experiences:
        log("No experiences selected")
        return

    attach_tripster_common_tags(db, experiences)
    log(f"selected={len(experiences)}, batch_size={args.batch_size}, workers={args.workers}, model={args.model}")
    allowed_set = set(allowed_sub_slugs)
    saved = 0
    batches = chunked(experiences, args.batch_size)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(
                process_batch,
                db=db,
                batch=batch,
                allowed_sub_slugs=allowed_sub_slugs,
                allowed_set=allowed_set,
                catalog_ids_by_sub_slug=catalog_ids_by_sub_slug,
                api_key=api_key,
                args=args,
            )
            for batch in batches
        ]
        for future in as_completed(futures):
            saved += future.result()

    if not args.dry_run:
        log(f"Done: saved={saved}")


if __name__ == "__main__":
    main()
