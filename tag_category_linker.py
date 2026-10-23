from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests


REST_PAGE_SIZE = 1000
DEFAULT_TIMEOUT = 60.0
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"

SERVICE_TAG_SLUGS = {
    "all",
    "top",
    "novye",
    "nedorogie",
    "special_offers",
    "chto-eshe-posmotret",
    "interesnye-ekskursii",
}
SERVICE_TAG_NAMES = {
    "все",
    "лучшие",
    "новые",
    "недорогие",
    "тематические",
    "что ещё посмотреть",
    "что еще посмотреть",
    "интересные экскурсии",
}
PLACE_SOURCE_CATEGORIES = {
    "достопримечательности",
    "из экскурсий",
}
PLACE_NAME_RE = re.compile(
    r"\b(остров|коса|замок|форт|крепость|собор|музей|башня|ворота|озеро|"
    r"бухта|район|парк|стадион|комбинат|синагога|кирха|маяк|деревня)\b",
    re.IGNORECASE,
)
ALWAYS_INCLUDE_MAIN_SLUGS = {
    "tip",
    "format",
    "na-chem",
    "vyezdnye",
    "vodnye-razvlecheniya",
    "razvlecheniya",
    "vremya-dnya-i-goda",
    "pod-nastroenie",
}


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


def request_with_retries(
    method: str,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = 5,
    **kwargs: Any,
) -> requests.Response:
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response

            if attempt == max_attempts:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(30.0, 2 ** (attempt - 1) + random.random())
            time.sleep(delay)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == max_attempts:
                raise
            time.sleep(min(30.0, 2 ** (attempt - 1) + random.random()))

    if last_error:
        raise last_error
    raise RuntimeError("request failed")


class SupabaseRest:
    def __init__(self, url: str, service_role_key: str) -> None:
        self.base_url = f"{url.rstrip('/')}/rest/v1"
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
        rows: list[dict[str, Any]] = []
        offset = 0

        while True:
            params: dict[str, Any] = {"select": select, "limit": REST_PAGE_SIZE, "offset": offset}
            if filters:
                params.update(filters)
            if order:
                params["order"] = order

            response = request_with_retries("GET", f"{self.base_url}/{table}", headers=self.headers, params=params)
            page = response.json()
            if not isinstance(page, list):
                raise RuntimeError(f"Unexpected Supabase response for {table}: {page!r}")

            rows.extend(page)
            if len(page) < REST_PAGE_SIZE:
                return rows
            offset += REST_PAGE_SIZE

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
        if not rows:
            return

        headers = {**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
        for index in range(0, len(rows), 500):
            batch = rows[index : index + 500]
            request_with_retries(
                "POST",
                f"{self.base_url}/{table}",
                headers=headers,
                params={"on_conflict": on_conflict},
                json=batch,
            )

    def rpc(self, function_name: str, payload: dict[str, Any]) -> Any:
        response = request_with_retries(
            "POST",
            f"{self.base_url}/rpc/{function_name}",
            headers=self.headers,
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        )
        return response.json()


def supabase_from_env() -> SupabaseRest:
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required in .env")
    return SupabaseRest(url, key)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().lower())


def classify_tag_kind(tag: dict[str, Any], city_name: str | None = None) -> str:
    name = normalize_text(tag.get("name"))
    tag_slug = normalize_text(tag.get("tag_slug") or tag.get("slug"))
    category = normalize_text(tag.get("category"))
    tag_category = normalize_text(tag.get("tag_category"))

    if tag_slug in SERVICE_TAG_SLUGS or name in SERVICE_TAG_NAMES:
        return "service"
    if tag.get("is_attraction") is True:
        return "place"
    if category in PLACE_SOURCE_CATEGORIES or tag_category in PLACE_SOURCE_CATEGORIES:
        return "place"
    if city_name and normalize_text(city_name) == name:
        return "place"
    if PLACE_NAME_RE.search(tag.get("name") or "") and category not in {"форматы", "топ"}:
        return "place"
    return "category_candidate"


def get_city(supabase: SupabaseRest, *, city_id: int | None, city_slug: str | None) -> dict[str, Any]:
    if city_id is not None:
        rows = supabase.select_all(
            "cities",
            "id,name_ru,name_en,slug",
            filters={"id": f"eq.{city_id}"},
        )
    elif city_slug:
        rows = supabase.select_all(
            "cities",
            "id,name_ru,name_en,slug",
            filters={"slug": f"eq.{city_slug}"},
        )
    else:
        raise RuntimeError("city_id or city_slug is required")

    if not rows:
        raise RuntimeError("City not found")
    if len(rows) > 1:
        raise RuntimeError(f"Expected one city, got {len(rows)}")
    return rows[0]


def fetch_tags(supabase: SupabaseRest, city_id: int | None) -> list[dict[str, Any]]:
    filters = {"city_id": f"eq.{city_id}"} if city_id is not None else None
    return supabase.select_all(
        "tags",
        "citytag_id,city_id,name,slug,tag_slug,category,tag_category,is_attraction,is_hidden,experience_count",
        filters=filters,
        order="experience_count.desc.nullslast,name.asc",
    )


def fetch_tripster_tags(supabase: SupabaseRest) -> list[dict[str, Any]]:
    return supabase.select_all(
        "tripster_tags",
        "id,name,slug,category,is_common,is_auto,is_seo_only,is_attraction",
        order="id.asc",
    )


def fetch_catalog(supabase: SupabaseRest) -> list[dict[str, Any]]:
    return supabase.select_all(
        "tag_category_catalog",
        "id,main_slug,main_name,sub_slug,sub_name",
        order="main_sort_order.asc,sub_sort_order.asc,main_name.asc,sub_name.asc",
    )


def fetch_linked_tag_ids(supabase: SupabaseRest) -> set[int]:
    rows = supabase.select_all("tag_category_links", "tag_id")
    return {int(row["tag_id"]) for row in rows if row.get("tag_id") is not None}


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file, delimiter=";"))


def export_tripster_unique(args: argparse.Namespace) -> None:
    supabase = supabase_from_env()
    tags = fetch_tripster_tags(supabase)

    rows: list[dict[str, Any]] = []
    for tag in tags:
        kind = classify_tag_kind(tag)
        if kind != "category_candidate":
            continue
        rows.append(
            {
                "approve": "",
                "tag_id": tag["id"],
                "city_id": "",
                "city_name": "",
                "citytag_id": "",
                "name": tag.get("name") or "",
                "tag_slug": tag.get("slug") or "",
                "category": tag.get("category") or "",
                "tag_category": "",
                "is_attraction": tag.get("is_attraction"),
                "experience_count": "",
                "kind_guess": kind,
                "reusable_key_type": "tag_id",
                "reusable_key": tag["id"],
                "seen_in_cities": "",
                "source_rows": "",
                "total_experience_count": "",
                "main_slug": "",
                "sub_slug": "",
                "confidence": "",
                "reason": "",
            }
        )

    write_csv(
        Path(args.out),
        rows,
        [
            "approve",
            "tag_id",
            "city_id",
            "city_name",
            "citytag_id",
            "name",
            "tag_slug",
            "category",
            "tag_category",
            "is_attraction",
            "experience_count",
            "kind_guess",
            "reusable_key_type",
            "reusable_key",
            "seen_in_cities",
            "source_rows",
            "total_experience_count",
            "main_slug",
            "sub_slug",
            "confidence",
            "reason",
        ],
    )
    print(f"Wrote {len(rows)} unique tripster category candidates to {args.out}")


def export_tripster_city_scope(args: argparse.Namespace) -> None:
    supabase = supabase_from_env()
    city_ids = [int(item.strip()) for item in args.city_ids.split(",") if item.strip()]
    if not city_ids:
        raise RuntimeError("--city-ids is required")

    filters = {"city_id": f"in.({','.join(str(city_id) for city_id in city_ids)})"}
    city_tag_rows = supabase.select_all(
        "tripster_city_tags",
        "citytag_id,tag_id,city_id,name,slug,category,experience_count",
        filters=filters,
        order="city_id.asc,name.asc",
    )
    city_names = {
        int(row["id"]): row.get("name_ru") or row.get("name_en") or str(row["id"])
        for row in supabase.select_all("cities", "id,name_ru,name_en", filters={"id": filters["city_id"]})
    }
    tags_by_id = {int(row["id"]): row for row in fetch_tripster_tags(supabase)}

    grouped: dict[int, dict[str, Any]] = {}
    for city_tag in city_tag_rows:
        tag_id = int(city_tag["tag_id"])
        tag = tags_by_id.get(tag_id)
        if not tag:
            continue
        entry = grouped.setdefault(
            tag_id,
            {
                "approve": "",
                "tag_id": tag_id,
                "name": tag.get("name") or city_tag.get("name") or "",
                "tag_slug": tag.get("slug") or city_tag.get("slug") or "",
                "category": tag.get("category") or city_tag.get("category") or "",
                "tag_category": "",
                "is_attraction": tag.get("is_attraction"),
                "kind_guess": classify_tag_kind(tag),
                "city_ids": [],
                "city_names": [],
                "citytag_ids": [],
                "source_rows": 0,
                "total_experience_count": 0,
                "main_slug": "",
                "sub_slug": "",
                "confidence": "",
                "reason": "",
            },
        )
        city_id = int(city_tag["city_id"])
        entry["city_ids"].append(str(city_id))
        entry["city_names"].append(city_names.get(city_id, str(city_id)))
        entry["citytag_ids"].append(str(city_tag["citytag_id"]))
        entry["source_rows"] += 1
        entry["total_experience_count"] += int(city_tag.get("experience_count") or 0)

    rows = []
    for entry in grouped.values():
        if args.only_kind and entry["kind_guess"] != args.only_kind:
            continue
        entry["city_ids"] = ",".join(dict.fromkeys(entry["city_ids"]))
        entry["city_names"] = ", ".join(dict.fromkeys(entry["city_names"]))
        entry["citytag_ids"] = ",".join(entry["citytag_ids"])
        rows.append(entry)

    rows.sort(key=lambda row: (row["kind_guess"], row["name"]))
    write_csv(
        Path(args.out),
        rows,
        [
            "approve",
            "tag_id",
            "name",
            "tag_slug",
            "category",
            "tag_category",
            "is_attraction",
            "kind_guess",
            "city_ids",
            "city_names",
            "citytag_ids",
            "source_rows",
            "total_experience_count",
            "main_slug",
            "sub_slug",
            "confidence",
            "reason",
        ],
    )
    print(f"Wrote {len(rows)} unique city-scope tripster tags to {args.out}")


def export_candidates(args: argparse.Namespace) -> None:
    supabase = supabase_from_env()
    city = get_city(supabase, city_id=args.city_id, city_slug=args.city_slug)
    tags = fetch_tags(supabase, int(city["id"]))
    all_tags = fetch_tags(supabase, None)

    tag_slug_city_counts: dict[str, set[int]] = {}
    name_city_counts: dict[str, set[int]] = {}
    for tag in all_tags:
        if tag.get("tag_slug"):
            tag_slug_city_counts.setdefault(str(tag["tag_slug"]), set()).add(int(tag["city_id"]))
        if tag.get("name"):
            name_city_counts.setdefault(normalize_text(tag["name"]), set()).add(int(tag["city_id"]))

    city_name = city.get("name_ru") or city.get("name_en")
    rows: list[dict[str, Any]] = []
    for tag in tags:
        name_key = normalize_text(tag.get("name"))
        tag_slug = tag.get("tag_slug") or ""
        kind = classify_tag_kind(tag, city_name=city_name)
        reusable_key = tag_slug or name_key
        reusable_key_type = "tag_slug" if tag_slug else "name"
        seen_in_cities = len(tag_slug_city_counts.get(tag_slug, set())) if tag_slug else len(name_city_counts.get(name_key, set()))

        rows.append(
            {
                "approve": "",
                "city_id": tag["city_id"],
                "city_name": city_name,
                "citytag_id": tag["citytag_id"],
                "name": tag.get("name") or "",
                "tag_slug": tag_slug,
                "category": tag.get("category") or "",
                "tag_category": tag.get("tag_category") or "",
                "is_attraction": tag.get("is_attraction"),
                "experience_count": tag.get("experience_count") or 0,
                "kind_guess": kind,
                "reusable_key_type": reusable_key_type,
                "reusable_key": reusable_key,
                "seen_in_cities": seen_in_cities,
                "main_slug": "",
                "sub_slug": "",
                "confidence": "",
                "reason": "",
            }
        )

    write_csv(
        Path(args.out),
        rows,
        [
            "approve",
            "city_id",
            "city_name",
            "citytag_id",
            "name",
            "tag_slug",
            "category",
            "tag_category",
            "is_attraction",
            "experience_count",
            "kind_guess",
            "reusable_key_type",
            "reusable_key",
            "seen_in_cities",
            "main_slug",
            "sub_slug",
            "confidence",
            "reason",
        ],
    )
    print(f"Exported {len(rows)} candidates to {args.out}")


def catalog_for_prompt(catalog: list[dict[str, Any]]) -> str:
    lines = []
    for row in catalog:
        lines.append(f"- {row['main_slug']} > {row['sub_slug']}: {row['main_name']} > {row['sub_name']}")
    return "\n".join(lines)


def catalog_for_llm(catalog: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "main_slug": str(row["main_slug"]),
            "main_name": str(row["main_name"]),
            "sub_slug": str(row["sub_slug"]),
            "sub_name": str(row["sub_name"]),
            "pair": f"{row['main_slug']} > {row['sub_slug']}",
        }
        for row in catalog
    ]


def compact_catalog_for_llm(catalog: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{row['main_slug']}/{row['sub_slug']}={row['main_name']} / {row['sub_name']}"
        for row in catalog
    )


def strip_code_fence(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def llm_settings(args: argparse.Namespace) -> tuple[str, str, str]:
    if os.environ.get("LLM_API_KEY"):
        api_key = os.environ["LLM_API_KEY"]
        api_base = os.environ.get("LLM_API_BASE", "https://api.openai.com/v1")
        model = args.model or os.environ.get("LLM_MODEL", DEFAULT_OPENAI_MODEL)
        return api_key, api_base, model

    if os.environ.get("OPENAI_API_KEY"):
        api_key = os.environ["OPENAI_API_KEY"]
        api_base = os.environ.get("LLM_API_BASE", "https://api.openai.com/v1")
        model = args.model or os.environ.get("LLM_MODEL", DEFAULT_OPENAI_MODEL)
        return api_key, api_base, model

    raise RuntimeError("Set LLM_API_KEY or OPENAI_API_KEY")


def fallback_llm_settings(args: argparse.Namespace) -> tuple[str, str, str] | None:
    api_key = os.environ.get("LLM_FALLBACK_API_KEY")
    api_base = os.environ.get("LLM_FALLBACK_API_BASE")
    model = args.model or os.environ.get("LLM_FALLBACK_MODEL")
    if api_key and api_base and model:
        return api_key, api_base, model
    return None


def embedding_settings(args: argparse.Namespace) -> tuple[str, str, str]:
    api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_FALLBACK_API_KEY")
    api_base = os.environ.get("EMBEDDING_API_BASE") or os.environ.get("LLM_FALLBACK_API_BASE") or "https://openrouter.ai/api/v1"
    model = args.model or os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
    if not api_key:
        raise RuntimeError("Set EMBEDDING_API_KEY or LLM_FALLBACK_API_KEY")
    return api_key, api_base, model


def request_embeddings(
    settings: tuple[str, str, str],
    inputs: list[str],
    timeout: float,
    max_attempts: int,
) -> list[list[float]]:
    api_key, api_base, model = settings
    response = request_with_retries(
        "POST",
        f"{api_base.rstrip('/')}/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": inputs,
        },
        timeout=timeout,
        max_attempts=max_attempts,
    )
    data = response.json()
    return [item["embedding"] for item in sorted(data["data"], key=lambda item: item["index"])]


def pgvector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8g}" for value in values) + "]"


def catalog_embedding_content(row: dict[str, Any]) -> str:
    triggers = ", ".join(str(value) for value in (row.get("lexical_triggers") or []))
    return f"{row['sub_name']}; {triggers}" if triggers else str(row["sub_name"])


def tag_embedding_content(row: dict[str, Any]) -> str:
    return (
        f"{row.get('name', '')}; "
        f"{row.get('tag_slug', '')}; "
        f"{row.get('category', '')}"
    )


def matching_catalog_for_tag(
    supabase: SupabaseRest,
    row: dict[str, str],
    settings: tuple[str, str, str],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    embedding = request_embeddings(settings, [tag_embedding_content(row)], args.timeout, args.max_retries)[0]
    return supabase.rpc(
        "match_tag_category_catalog_embeddings",
        {
            "p_embedding": pgvector_literal(embedding),
            "p_model": settings[2],
            "p_limit": args.catalog_top_k,
        },
    )


def expand_catalog_matches(
    matched_catalog: list[dict[str, Any]],
    full_catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    matched_sub_slugs = {str(row.get("sub_slug") or "") for row in matched_catalog}
    matched_sub_names = {normalize_text(str(row.get("sub_name") or "")) for row in matched_catalog}

    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in [*matched_catalog, *full_catalog]:
        pair = (str(row.get("main_slug") or ""), str(row.get("sub_slug") or ""))
        if pair in seen:
            continue
        if row in matched_catalog or (
            str(row.get("sub_slug") or "") in matched_sub_slugs
            or normalize_text(str(row.get("sub_name") or "")) in matched_sub_names
            or str(row.get("main_slug") or "") in ALWAYS_INCLUDE_MAIN_SLUGS
        ):
            output.append(row)
            seen.add(pair)
    return output


def build_llm_messages(tag: dict[str, str], catalog_items: str | list[dict[str, str]]) -> list[dict[str, str]]:
    system = (
        "Классифицируй Tripster-тег по закрытому списку фильтров. "
        "Выбирай только пары main/sub из списка, не придумывай slug. "
        "Верни все применимые пары: тип, формат, тематику, транспорт, время, настроение, выездность, водные/развлекательные признаки. "
        "Один тег часто должен попасть в несколько верхних категорий. "
        "Не ограничивайся одной парой, если тег описывает несколько фильтров. "
        "Пустой список возвращай только если среди ALLOWED вообще нет подходящих фильтров. "
        "Места классифицируй по типу места, а не по формату экскурсии. "
        "Теги городов/направлений вроде 'Абу-Даби' или 'Аджман' для другого города относить к выездным/за городом, если такие пары есть. "
        "Городские острова/районы/улицы не относить к природным островам. "
        "Ответ строго JSON: {\"selected_categories\":[{\"main_slug\":\"...\",\"sub_slug\":\"...\"}]}"
    )
    user = (
        "TAG:\n"
        f"name={tag.get('name', '')}\n"
        f"slug={tag.get('tag_slug', '')}\n"
        f"category={tag.get('category', '')}\n"
        f"is_attraction={tag.get('is_attraction', '')}\n\n"
        "ALLOWED main/sub=names:\n"
        f"{catalog_items}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def compact_tag_for_llm(tag: dict[str, str]) -> dict[str, str]:
    return {
        "analysis_key": tag.get("reusable_key") or tag.get("tag_slug") or tag.get("citytag_id", ""),
        "city_id": tag.get("city_id", ""),
        "city_name": tag.get("city_name", ""),
        "citytag_id": tag.get("citytag_id", ""),
        "name": tag.get("name", ""),
        "tag_slug": tag.get("tag_slug", ""),
        "source_category": tag.get("category", ""),
        "source_tag_category": tag.get("tag_category", ""),
        "is_attraction": tag.get("is_attraction", ""),
        "experience_count": tag.get("experience_count", ""),
        "kind_guess": tag.get("kind_guess", ""),
        "seen_in_cities": tag.get("seen_in_cities", ""),
        "source_rows": tag.get("source_rows", ""),
        "total_experience_count": tag.get("total_experience_count", ""),
    }


def build_llm_batch_messages(tags: list[dict[str, str]], catalog_items: list[dict[str, str]]) -> list[dict[str, str]]:
    system = (
        "Ты классифицируешь теги экскурсий по закрытому справочнику категорий. "
        "На входе будет массив tags. Для каждого тега нужно вернуть один result с тем же analysis_key. "
        "Выбирай только существующие пары main_slug/sub_slug из allowed_categories. "
        "Нельзя придумывать новые slug, новые категории или менять написание slug. "
        "Если тег является конкретным местом или достопримечательностью, классифицируй его по типу места. "
        "Для места не выбирай формат экскурсии вроде 'обзорные', 'индивидуальные', 'групповые', если это не свойство самого тега. "
        "Городские острова, районы, улицы и площади не являются природной категорией 'На острова': для них выбирай исторические, архитектурные или популярные места. "
        "Для служебных витринных тегов вроде 'Все', 'Лучшие', 'Новые', 'Что ещё посмотреть' верни tag_kind='service' и пустой selected_categories. "
        "Для универсальных тегов, которые можно применять в других городах, ставь is_reusable=true. "
        "Для конкретных мест обычно ставь is_reusable=false. "
        "Для каждого тега возвращай все релевантные selected_categories для фильтров. "
        "Один тег может относиться к нескольким верхним категориям и подкатегориям. "
        "Не ограничивайся искусственным числом связей, но не добавляй слабые или почти дублирующие связи без смысла. "
        "Верни строго JSON без markdown."
    )
    user = {
        "task": {
            "goal": "Выбрать все релевантные категории фильтров для каждого Tripster tag.",
            "rules": [
                "Верни ровно один result на каждый входной tag.",
                "Сохрани analysis_key из входного tag в результате.",
                "Используй только пары из allowed_categories.",
                "Если уверенной связи нет, верни пустой selected_categories.",
                "Один тег может относиться к нескольким фильтрам.",
                "Не добавляй слабые связи только ради количества.",
                "reason пиши коротко по-русски.",
            ],
        },
        "tags": [compact_tag_for_llm(tag) for tag in tags],
        "allowed_categories": catalog_items,
        "required_output_json": {
            "results": [
                {
                    "analysis_key": "same as input tag.analysis_key",
                    "tag_kind": "universal_category | place | service | unknown",
                    "is_reusable": "boolean",
                    "selected_categories": [
                        {
                            "main_slug": "exact main_slug from allowed_categories",
                            "sub_slug": "exact sub_slug from allowed_categories",
                            "confidence": "number from 0 to 1",
                            "reason": "short Russian explanation",
                        }
                    ],
                }
            ]
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def request_llm(
    settings: tuple[str, str, str],
    messages: list[dict[str, str]],
    timeout: float,
    max_attempts: int,
) -> dict[str, Any]:
    api_key, api_base, model = settings
    url = f"{api_base.rstrip('/')}/chat/completions"
    response = request_with_retries(
        "POST",
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": messages,
            "temperature": 0,
        },
        timeout=timeout,
        max_attempts=max_attempts,
    )
    content = strip_code_fence(response.json()["choices"][0]["message"]["content"])
    return json.loads(content)


def call_llm(tag: dict[str, str], catalog_items: list[dict[str, str]], args: argparse.Namespace) -> dict[str, Any]:
    messages = build_llm_messages(tag, catalog_items)
    primary = llm_settings(args)
    try:
        return request_llm(primary, messages, args.timeout, args.max_retries)
    except Exception:
        if not getattr(args, "use_fallback", False):
            raise
        fallback = fallback_llm_settings(args)
        if not fallback:
            raise
        return request_llm(fallback, messages, args.timeout, args.max_retries)


def call_llm_batch(tags: list[dict[str, str]], catalog_items: list[dict[str, str]], args: argparse.Namespace) -> dict[str, Any]:
    messages = build_llm_batch_messages(tags, catalog_items)
    primary = llm_settings(args)
    try:
        return request_llm(primary, messages, args.timeout, args.max_retries)
    except Exception:
        if not getattr(args, "use_fallback", False):
            raise
        fallback = fallback_llm_settings(args)
        if not fallback:
            raise
        return request_llm(fallback, messages, args.timeout, args.max_retries)


def proposal_to_rows(
    row: dict[str, str],
    proposal: dict[str, Any],
    allowed_pairs: set[tuple[str | None, str | None]],
) -> list[dict[str, Any]]:
    links = proposal.get("selected_categories") or proposal.get("links") or []
    if not links:
        return [
            {
                **row,
                "llm_kind": proposal.get("tag_kind", ""),
                "llm_reusable": proposal.get("is_reusable", ""),
                "reason": "",
            }
        ]

    result: list[dict[str, Any]] = []
    for link in links:
        pair = (link.get("main_slug"), link.get("sub_slug"))
        if pair not in allowed_pairs:
            continue
        result.append(
            {
                **row,
                "llm_kind": proposal.get("tag_kind", ""),
                "llm_reusable": proposal.get("is_reusable", ""),
                "main_slug": link.get("main_slug", ""),
                "sub_slug": link.get("sub_slug", ""),
                "confidence": link.get("confidence", ""),
                "reason": link.get("reason", ""),
            }
        )
    return result


def batch_proposal_to_rows(
    rows_by_key: dict[str, dict[str, str]],
    proposal: dict[str, Any],
    allowed_pairs: set[tuple[str | None, str | None]],
) -> list[dict[str, Any]]:
    results = proposal.get("results") or []
    output: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for item in results:
        key = str(item.get("analysis_key", ""))
        row = rows_by_key.get(key)
        if not row:
            continue
        seen_keys.add(key)
        output.extend(proposal_to_rows(row, item, allowed_pairs))

    for key, row in rows_by_key.items():
        if key not in seen_keys:
            output.append({**row, "llm_kind": "error", "llm_reusable": "", "reason": "missing result in batch response"})

    return output


def batched_items(rows: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def propose_llm(args: argparse.Namespace) -> None:
    supabase = supabase_from_env()
    catalog = fetch_catalog(supabase)
    catalog_items = compact_catalog_for_llm(catalog)
    allowed_pairs = {(row["main_slug"], row["sub_slug"]) for row in catalog}

    input_rows = read_csv(Path(args.input))
    candidates = [row for row in input_rows if args.include_service or row.get("kind_guess") != "service"]
    if args.only_kind:
        candidates = [row for row in candidates if row.get("kind_guess") == args.only_kind]
    if args.contains:
        needle = normalize_text(args.contains)
        candidates = [row for row in candidates if needle in normalize_text(row.get("name"))]
    candidates = candidates[: args.limit] if args.limit else candidates

    def process(index: int, row: dict[str, str]) -> tuple[int, list[dict[str, Any]]]:
        proposal = call_llm(row, catalog_items, args)
        return index, proposal_to_rows(row, proposal, allowed_pairs)

    fieldnames = list(input_rows[0].keys()) + ["llm_kind", "llm_reusable"]
    fieldnames = list(dict.fromkeys(fieldnames))
    output_path = Path(args.out)
    written_rows = 0
    completed = 0

    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process, index, row): (index, row) for index, row in enumerate(candidates)}
            for future in as_completed(futures):
                index, row = futures[future]
                try:
                    _, rows = future.result()
                except Exception as exc:
                    rows = [{**row, "llm_kind": "error", "llm_reusable": "", "reason": str(exc)}]

                for result_row in rows:
                    writer.writerow(result_row)
                    written_rows += 1
                file.flush()

                completed += 1
                print(
                    f"LLM processed {completed}/{len(candidates)}: {row.get('name')} "
                    f"(written rows: {written_rows})",
                    flush=True,
                )

    print(f"Wrote {written_rows} proposals to {args.out}")


def link_rows_for_proposals(
    proposal_rows: list[dict[str, Any]],
    catalog_by_pair: dict[tuple[str, str], dict[str, Any]],
    tags_by_slug: dict[str, list[dict[str, Any]]],
    tags_by_name: dict[str, list[dict[str, Any]]],
    existing_links: set[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    link_rows: list[dict[str, Any]] = []
    existing_links = existing_links or set()

    for row in proposal_rows:
        if row.get("llm_kind") == "error":
            continue
        main_slug = str(row.get("main_slug") or "")
        sub_slug = str(row.get("sub_slug") or "")
        if not main_slug or not sub_slug:
            continue
        catalog_row = catalog_by_pair.get((main_slug, sub_slug))
        if not catalog_row:
            continue

        if row.get("tag_slug"):
            target_tags = tags_by_slug.get(str(row["tag_slug"]), [])
        else:
            target_tags = tags_by_name.get(normalize_text(str(row.get("name") or "")), [])

        for tag in target_tags:
            if classify_tag_kind(tag) != "category_candidate":
                continue
            key = (int(tag["id"]), int(catalog_row["id"]))
            if key in existing_links:
                continue
            link_rows.append(
                {
                    "tag_id": tag["id"],
                    "catalog_id": catalog_row["id"],
                }
            )

    return link_rows


def propose_and_apply_unique(args: argparse.Namespace) -> None:
    supabase = supabase_from_env()
    catalog = fetch_catalog(supabase)
    allowed_pairs = {(row["main_slug"], row["sub_slug"]) for row in catalog}
    catalog_by_pair = {(row["main_slug"], row["sub_slug"]): row for row in catalog}
    default_catalog_items = compact_catalog_for_llm(catalog)
    embedding_cfg = embedding_settings(args) if args.catalog_top_k else None

    input_rows = read_csv(Path(args.input))
    candidates = [row for row in input_rows if row.get("kind_guess") == "category_candidate"]
    if args.contains:
        needle = normalize_text(args.contains)
        candidates = [row for row in candidates if needle in normalize_text(row.get("name"))]

    skipped_linked = 0
    if not args.dry_run:
        linked_tag_ids = fetch_linked_tag_ids(supabase)
        before_skip = len(candidates)
        candidates = [
            row
            for row in candidates
            if not row.get("tag_id") or int(row["tag_id"]) not in linked_tag_ids
        ]
        skipped_linked = before_skip - len(candidates)

    candidates = candidates[: args.limit] if args.limit else candidates
    if skipped_linked:
        print(f"Skipped already linked tags: {skipped_linked}", flush=True)

    completed = 0
    total_links = 0

    def process(row: dict[str, str]) -> tuple[dict[str, str], int, list[str], str | None]:
        try:
            if embedding_cfg:
                matched_catalog = matching_catalog_for_tag(supabase, row, embedding_cfg, args)
                matched_catalog = expand_catalog_matches(matched_catalog, catalog)
                catalog_items = compact_catalog_for_llm(matched_catalog)
            else:
                catalog_items = default_catalog_items
            proposal = call_llm(row, catalog_items, args)
            proposal_rows = proposal_to_rows(row, proposal, allowed_pairs)
            inserted_total = 0
            selected_names: list[str] = []
            seen_selected: set[tuple[str, str]] = set()
            for proposal_row in proposal_rows:
                main_slug = str(proposal_row.get("main_slug") or "")
                sub_slug = str(proposal_row.get("sub_slug") or "")
                catalog_row = catalog_by_pair.get((main_slug, sub_slug))
                if not catalog_row:
                    continue
                selected_key = (main_slug, sub_slug)
                if selected_key in seen_selected:
                    continue
                seen_selected.add(selected_key)
                selected_names.append(f"{catalog_row['main_name']} / {catalog_row['sub_name']}")

            if not args.dry_run:
                for proposal_row in proposal_rows:
                    main_slug = str(proposal_row.get("main_slug") or "")
                    sub_slug = str(proposal_row.get("sub_slug") or "")
                    if not main_slug or not sub_slug:
                        continue
                    catalog_row = catalog_by_pair.get((main_slug, sub_slug))
                    if not catalog_row:
                        continue
                    inserted = supabase.rpc(
                        "apply_tag_category_link_by_key",
                        {
                            "p_tag_slug": row.get("tag_slug") or None,
                            "p_name": row.get("name") or None,
                            "p_catalog_id": catalog_row["id"],
                        },
                    )
                    inserted_total += int(inserted or 0)
            else:
                inserted_total = len(selected_names)
            return row, inserted_total, selected_names, None
        except Exception as exc:
            return row, 0, [], str(exc)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process, row): row for row in candidates}
        for future in as_completed(futures):
            row, inserted_count, selected_names, error = future.result()
            completed += 1

            if error:
                print(f"LLM apply {completed}/{len(candidates)}: {row.get('name')} ERROR {error}", flush=True)
                continue

            if args.dry_run:
                total_links += inserted_count
            else:
                total_links += inserted_count

            print(
                f"LLM apply {completed}/{len(candidates)}: {row.get('name')} "
                f"-> {', '.join(selected_names) if selected_names else 'no categories'} "
                f"(inserted links: {inserted_count}, total inserted: {total_links})",
                flush=True,
            )

    mode = "would upsert" if args.dry_run else "upserted"
    print(f"Done: {mode} {total_links} tag_category_links")


def propose_llm_batch(args: argparse.Namespace) -> None:
    supabase = supabase_from_env()
    catalog = fetch_catalog(supabase)
    catalog_items = catalog_for_llm(catalog)
    allowed_pairs = {(row["main_slug"], row["sub_slug"]) for row in catalog}

    input_rows = read_csv(Path(args.input))
    candidates = [row for row in input_rows if args.include_service or row.get("kind_guess") != "service"]
    if args.only_kind:
        candidates = [row for row in candidates if row.get("kind_guess") == args.only_kind]
    if args.contains:
        needle = normalize_text(args.contains)
        candidates = [row for row in candidates if needle in normalize_text(row.get("name"))]
    candidates = candidates[: args.limit] if args.limit else candidates
    batches = batched_items(candidates, args.batch_size)

    fieldnames = list(input_rows[0].keys()) + ["llm_kind", "llm_reusable"]
    fieldnames = list(dict.fromkeys(fieldnames))
    output_path = Path(args.out)
    written_rows = 0
    completed = 0

    def process(batch_index: int, batch: list[dict[str, str]]) -> tuple[int, list[dict[str, Any]]]:
        rows_by_key = {
            (row.get("reusable_key") or row.get("tag_slug") or row.get("citytag_id", "")): row
            for row in batch
        }
        proposal = call_llm_batch(batch, catalog_items, args)
        return batch_index, batch_proposal_to_rows(rows_by_key, proposal, allowed_pairs)

    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process, index, batch): (index, batch) for index, batch in enumerate(batches)}
            for future in as_completed(futures):
                index, batch = futures[future]
                try:
                    _, rows = future.result()
                except Exception as exc:
                    rows = [
                        {**row, "llm_kind": "error", "llm_reusable": "", "reason": str(exc)}
                        for row in batch
                    ]

                for result_row in rows:
                    writer.writerow(result_row)
                    written_rows += 1
                file.flush()

                completed += 1
                print(
                    f"LLM batch processed {completed}/{len(batches)} "
                    f"(tags: {len(batch)}, written rows: {written_rows})",
                    flush=True,
                )

    print(f"Wrote {written_rows} proposals to {args.out}")


def truthy(value: str | None) -> bool:
    return normalize_text(value) in {"1", "true", "yes", "y", "да", "ok", "approved", "approve"}


def apply_approved(args: argparse.Namespace) -> None:
    supabase = supabase_from_env()
    rows = read_csv(Path(args.input))
    catalog = fetch_catalog(supabase)
    catalog_by_pair = {(row["main_slug"], row["sub_slug"]): row for row in catalog}

    approved = [row for row in rows if truthy(row.get("approve")) and row.get("main_slug") and row.get("sub_slug")]
    if not approved:
        print("No approved rows to apply")
        return

    tripster_tags = fetch_tripster_tags(supabase)
    tags_by_slug: dict[str, list[dict[str, Any]]] = {}
    tags_by_name: dict[str, list[dict[str, Any]]] = {}
    for tag in tripster_tags:
        if tag.get("slug"):
            tags_by_slug.setdefault(str(tag["slug"]), []).append(tag)
        if tag.get("name"):
            tags_by_name.setdefault(normalize_text(str(tag["name"])), []).append(tag)

    link_rows: list[dict[str, Any]] = []
    for row in approved:
        pair = (row["main_slug"], row["sub_slug"])
        catalog_row = catalog_by_pair.get(pair)
        if not catalog_row:
            print(f"Skip unknown catalog pair: {pair}", file=sys.stderr)
            continue

        if row.get("tag_slug"):
            target_tags = tags_by_slug.get(str(row["tag_slug"]), [])
        else:
            target_tags = tags_by_name.get(normalize_text(str(row.get("name") or "")), [])

        for tag in target_tags:
            if classify_tag_kind(tag) != "category_candidate":
                continue
            link_rows.append(
                {
                    "tag_id": tag["id"],
                    "catalog_id": catalog_row["id"],
                }
            )

    if args.dry_run:
        print(f"Dry run: would upsert {len(link_rows)} links")
        return

    supabase.upsert("tag_category_links", link_rows, "tag_id,catalog_id")
    print(f"Upserted {len(link_rows)} approved links")


def build_catalog_embeddings(args: argparse.Namespace) -> None:
    supabase = supabase_from_env()
    catalog = supabase.select_all(
        "tag_category_catalog",
        "id,sub_name,lexical_triggers",
        filters={"is_common": "eq.true"},
        order="main_sort_order.asc,sub_sort_order.asc,main_name.asc,sub_name.asc",
    )
    settings = embedding_settings(args)
    model = settings[2]

    if args.missing_only:
        existing = {
            int(row["catalog_id"])
            for row in supabase.select_all(
                "tag_category_catalog_embeddings",
                "catalog_id,model",
                filters={"model": f"eq.{model}"},
            )
            if row.get("catalog_id") is not None
        }
        catalog = [row for row in catalog if int(row["id"]) not in existing]

    catalog = catalog[: args.limit] if args.limit else catalog
    if not catalog:
        print("No catalog embeddings to build")
        return

    written = 0
    for index in range(0, len(catalog), args.batch_size):
        batch = catalog[index : index + args.batch_size]
        contents = [catalog_embedding_content(row) for row in batch]
        embeddings = request_embeddings(settings, contents, args.timeout, args.max_retries)
        rows = [
            {
                "catalog_id": row["id"],
                "content": content,
                "model": model,
                "embedding": pgvector_literal(embedding),
            }
            for row, content, embedding in zip(batch, contents, embeddings)
        ]
        if not args.dry_run:
            supabase.upsert("tag_category_catalog_embeddings", rows, "catalog_id")
        written += len(rows)
        print(f"Catalog embeddings {written}/{len(catalog)}", flush=True)

    mode = "would write" if args.dry_run else "wrote"
    print(f"Done: {mode} {written} catalog embeddings with {model}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build reviewed links between Tripster tags and tag_category_catalog.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="Export city tag candidates to CSV for review or LLM proposal.")
    export.add_argument("--city-id", type=int)
    export.add_argument("--city-slug")
    export.add_argument("--out", required=True)
    export.set_defaults(func=export_candidates)

    export_tripster = subparsers.add_parser("export-tripster-unique", help="Export unique normalized tripster_tags candidates to CSV.")
    export_tripster.add_argument("--out", required=True)
    export_tripster.set_defaults(func=export_tripster_unique)

    export_city_scope = subparsers.add_parser("export-tripster-city-scope", help="Export unique tripster_tags used by selected cities.")
    export_city_scope.add_argument("--city-ids", required=True, help="Comma-separated city ids.")
    export_city_scope.add_argument("--only-kind", choices=["category_candidate", "place", "service"])
    export_city_scope.add_argument("--out", required=True)
    export_city_scope.set_defaults(func=export_tripster_city_scope)

    llm = subparsers.add_parser("llm", help="Use an OpenAI-compatible LLM to propose category links into a CSV.")
    llm.add_argument("--input", required=True)
    llm.add_argument("--out", required=True)
    llm.add_argument("--model")
    llm.add_argument("--limit", type=int)
    llm.add_argument("--workers", type=int, default=1)
    llm.add_argument("--timeout", type=float, default=30.0)
    llm.add_argument("--max-retries", type=int, default=1)
    llm.add_argument("--use-fallback", action="store_true")
    llm.add_argument("--include-service", action="store_true")
    llm.add_argument("--only-kind", choices=["category_candidate", "place", "service"])
    llm.add_argument("--contains")
    llm.set_defaults(func=propose_llm)

    llm_batch = subparsers.add_parser("llm-batch", help="Use an OpenAI-compatible LLM to propose category links in tag batches.")
    llm_batch.add_argument("--input", required=True)
    llm_batch.add_argument("--out", required=True)
    llm_batch.add_argument("--model")
    llm_batch.add_argument("--limit", type=int)
    llm_batch.add_argument("--workers", type=int, default=5)
    llm_batch.add_argument("--timeout", type=float, default=45.0)
    llm_batch.add_argument("--max-retries", type=int, default=1)
    llm_batch.add_argument("--use-fallback", action="store_true")
    llm_batch.add_argument("--batch-size", type=int, default=15)
    llm_batch.add_argument("--include-service", action="store_true")
    llm_batch.add_argument("--only-kind", choices=["category_candidate", "place", "service"])
    llm_batch.add_argument("--contains")
    llm_batch.set_defaults(func=propose_llm_batch)

    llm_apply = subparsers.add_parser("llm-apply-unique", help="Run LLM on unique category tags and immediately upsert links.")
    llm_apply.add_argument("--input", required=True)
    llm_apply.add_argument("--model")
    llm_apply.add_argument("--limit", type=int)
    llm_apply.add_argument("--workers", type=int, default=20)
    llm_apply.add_argument("--timeout", type=float, default=20.0)
    llm_apply.add_argument("--max-retries", type=int, default=1)
    llm_apply.add_argument("--use-fallback", action="store_true")
    llm_apply.add_argument("--contains")
    llm_apply.add_argument("--catalog-top-k", type=int, help="Use embeddings to send only top-K catalog categories to LLM.")
    llm_apply.add_argument("--dry-run", action="store_true")
    llm_apply.set_defaults(func=propose_and_apply_unique)

    apply = subparsers.add_parser("apply", help="Apply approved CSV rows to tag_category_links.")
    apply.add_argument("--input", required=True)
    apply.add_argument("--city-id", type=int)
    apply.add_argument("--reuse-global", action="store_true", help="Deprecated; tag-level links are already global.")
    apply.add_argument("--dry-run", action="store_true")
    apply.set_defaults(func=apply_approved)

    embeddings = subparsers.add_parser("build-catalog-embeddings", help="Build OpenRouter embeddings for tag_category_catalog.")
    embeddings.add_argument("--model")
    embeddings.add_argument("--batch-size", type=int, default=64)
    embeddings.add_argument("--timeout", type=float, default=30.0)
    embeddings.add_argument("--max-retries", type=int, default=2)
    embeddings.add_argument("--limit", type=int)
    embeddings.add_argument("--missing-only", action="store_true")
    embeddings.add_argument("--dry-run", action="store_true")
    embeddings.set_defaults(func=build_catalog_embeddings)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
