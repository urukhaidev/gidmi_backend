from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests


REST_PAGE_SIZE = 1000
DEFAULT_API_BASE = "https://api.aigate.shop/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_MAX_OUTPUT_TOKENS = 20000
RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def request_with_retries(
    method: str,
    url: str,
    *,
    timeout: float,
    max_attempts: int,
    **kwargs: Any,
) -> requests.Response:
    last_error: Exception | None = None
    response: requests.Response | None = None
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

        retry_after = response.headers.get("Retry-After") if response is not None else None
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
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        page_size = min(limit or REST_PAGE_SIZE, REST_PAGE_SIZE)
        while True:
            params: dict[str, Any] = {"select": select, "limit": page_size, "offset": offset}
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
            if len(page) < page_size or (limit is not None and len(rows) >= limit):
                return rows[:limit] if limit is not None else rows
            offset += page_size

    def patch_by_id(self, table: str, row_id: int, values: dict[str, Any]) -> None:
        request_with_retries(
            "PATCH",
            f"{self.base_url}/{table}",
            headers={**self.headers, "Prefer": "return=minimal"},
            params={"id": f"eq.{row_id}"},
            json=values,
            timeout=self.timeout,
            max_attempts=self.max_retries,
        )

    def insert(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        response = request_with_retries(
            "POST",
            f"{self.base_url}/{table}",
            headers={**self.headers, "Prefer": "return=representation"},
            json=values,
            timeout=self.timeout,
            max_attempts=self.max_retries,
        )
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise RuntimeError(f"Unexpected Supabase insert response for {table}: {payload!r}")
        if not isinstance(payload[0], dict):
            raise RuntimeError(f"Unexpected Supabase insert row for {table}: {payload[0]!r}")
        return payload[0]


def supabase_from_env(timeout: float, max_retries: int) -> SupabaseRest:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env")
    return SupabaseRest(url, key, timeout, max_retries)


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
                break
            except json.JSONDecodeError:
                continue
        else:
            raise
    if not isinstance(parsed, dict):
        raise RuntimeError(f"LLM JSON must be an object: {parsed!r}")
    return parsed


def clean_field(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"Expected string field {field}, got {value!r}")
    cleaned = value.strip().replace("\r\n", "\n")
    if not cleaned:
        raise RuntimeError(f"Field {field} is empty")
    return cleaned


def clean_faq(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuntimeError(f"Expected faq list, got {value!r}")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError(f"Expected faq item object, got {item!r}")
        question = clean_field(item.get("question"), "faq.question")
        answer = clean_field(item.get("answer"), "faq.answer")
        result.append({"question": question, "answer": answer})
    return result


def faq_for_storage(value: list[dict[str, str]]) -> str:
    return json.dumps(value, ensure_ascii=False)


def validate_output(fields: dict[str, Any]) -> dict[str, Any]:
    body = clean_field(fields.get("body"), "body")
    if "<p" not in body or "<h2" not in body:
        raise RuntimeError("body must be HTML with <h2> and <p> sections")
    return {
        "title": clean_field(fields.get("title"), "title"),
        "h1": clean_field(fields.get("h1"), "h1"),
        "h2": clean_field(fields.get("h2"), "h2"),
        "body": body,
        "faq": clean_faq(fields.get("faq")),
    }


def call_llm(
    *,
    messages: list[dict[str, str]],
    api_key: str,
    api_base: str,
    model: str,
    max_output_tokens: int,
    response_format: bool,
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.35,
        "max_tokens": max_output_tokens,
    }
    if response_format:
        payload["response_format"] = {"type": "json_object"}

    response = request_with_retries(
        "POST",
        f"{api_base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
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
    return parse_llm_json(str(content))


def money_rub(value: float | int | None) -> str | None:
    if value is None:
        return None
    return f"{int(value):,}".replace(",", " ") + " ₽"


def sum_int(rows: list[dict[str, Any]], key: str) -> int:
    total = 0
    for row in rows:
        value = row.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


def fetch_min_experience_price(db: SupabaseRest, filters: dict[str, str]) -> str | None:
    rows = db.select_all(
        "experiences",
        "price_value",
        filters={**filters, "status": "eq.active", "price_value": "gt.0"},
        order="price_value.asc",
        limit=1,
    )
    if not rows:
        return None
    value = rows[0].get("price_value")
    return money_rub(value) if isinstance(value, (int, float)) else None


def html_headings(html: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", item)).strip()
        for item in re.findall(r"<h2[^>]*>(.*?)</h2>", html, flags=re.IGNORECASE | re.DOTALL)
        if item.strip()
    ]


def fetch_tripster_city_reference(city: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    token = os.environ.get("TRIPSTER_TOKEN")
    slug = str(city.get("slug") or city.get("name_en") or "").strip()
    if args.no_tripster_reference or not token or not slug:
        return None

    response = request_with_retries(
        "GET",
        f"https://experience.tripster.ru/api/web/v1/cities/{slug}/seo/all/",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
        params={"format": "json", "page": 1},
        timeout=args.timeout,
        max_attempts=args.max_retries,
    )
    payload = response.json()
    if not isinstance(payload, dict):
        return None
    seo = payload.get("seo")
    if not isinstance(seo, dict):
        return None

    # Do not pass competitor body text to the LLM. Keep only structure and counters.
    return {
        "slug": seo.get("slug"),
        "title": seo.get("title"),
        "description": seo.get("description"),
        "h1": seo.get("h1"),
        "h2": seo.get("h2"),
        "headings": html_headings(str(seo.get("text") or "")),
        "faq_count": len(payload.get("faq") or []) if isinstance(payload.get("faq"), list) else None,
        "city_rating": payload.get("city_rating"),
        "review_count": payload.get("review_count"),
        "price_ranges": payload.get("price_ranges"),
    }


def location_slug(entity_type: str, entity_id: Any) -> str:
    return f"{entity_type}__{entity_type}__{entity_id}"


def fetch_existing_block(db: SupabaseRest, entity_type: str, entity_id: int) -> dict[str, Any] | None:
    rows = db.select_all(
        "seo_blocks",
        "id,entity_type,entity_id,h1,title,slug,h2,body,is_active,faq",
        filters={"entity_type": f"eq.{entity_type}", "entity_id": f"eq.{entity_id}"},
        order="id.asc",
        limit=1,
    )
    return rows[0] if rows else None


def fetch_completed_entity_ids(db: SupabaseRest, entity_type: str) -> set[int]:
    rows = db.select_all(
        "seo_blocks",
        "entity_id",
        filters={"entity_type": f"eq.{entity_type}", "body": "not.is.null"},
    )
    completed: set[int] = set()
    for row in rows:
        value = row.get("entity_id")
        try:
            completed.add(int(value))
        except (TypeError, ValueError):
            continue
    return completed


def fetch_city_targets(db: SupabaseRest, args: argparse.Namespace) -> list[dict[str, Any]]:
    filters: dict[str, str] = {}
    explicit_selection = bool(args.city_id or args.city_slug)
    if args.city_id:
        filters["id"] = f"in.({','.join(str(item) for item in args.city_id)})"
    elif args.city_slug:
        filters["slug"] = f"in.({','.join(args.city_slug)})"
    rows = db.select_all(
        "cities",
        (
            "id,name_ru,name_1_ru,name_5_ru,name_en,slug,country_id,experience_count,"
            "tours_count,excursions_count,guides_count,in_obj_phrase,about_obj_phrase,"
            "from_obj_phrase,across_obj_phrase,name_prepositional,url"
        ),
        filters=filters or None,
        order="experience_count.desc",
    )
    if not args.force and not explicit_selection:
        completed_ids = fetch_completed_entity_ids(db, "city")
        rows = [row for row in rows if int(row["id"]) not in completed_ids]
    rows = rows[: args.limit]
    return [{"entity_type": "city", "row": row} for row in rows]


def fetch_country_targets(db: SupabaseRest, args: argparse.Namespace) -> list[dict[str, Any]]:
    filters: dict[str, str] = {}
    explicit_selection = bool(args.country_id)
    if args.country_id:
        filters["id"] = f"in.({','.join(str(item) for item in args.country_id)})"
    rows = db.select_all(
        "countries",
        (
            "id,name_ru,name_2_ru,name_5_ru,name_en,currency,in_obj_phrase,about_obj_phrase,"
            "across_obj_phrase,experience_count,tours_count,guides_count,url,tours_url,region"
        ),
        filters=filters or None,
        order="experience_count.desc",
    )
    if not args.force and not explicit_selection:
        completed_ids = fetch_completed_entity_ids(db, "country")
        rows = [row for row in rows if int(row["id"]) not in completed_ids]
    rows = rows[: args.limit]
    return [{"entity_type": "country", "row": row} for row in rows]


def fetch_city_context(db: SupabaseRest, city: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    city_id = int(city["id"])
    tags = db.select_all(
        "tags",
        "name,category,experience_count,review_count,is_common,is_attraction,is_hidden",
        filters={"city_id": f"eq.{city_id}", "is_hidden": "eq.false"},
        order="experience_count.desc",
        limit=args.context_limit,
    )
    attractions = [row for row in tags if row.get("is_attraction")]
    common_tags = [row for row in tags if row.get("is_common") and not row.get("is_attraction")]
    experiences = db.select_all(
        "experiences",
        "title,tagline,annotation,rating,review_count,price_value,price_value_string,price_currency",
        filters={"city_id": f"eq.{city_id}", "status": "eq.active"},
        order="review_count.desc.nullslast",
        limit=args.context_limit,
    )
    return {
        "location": city,
        "stats": {
            "experience_count": city.get("experience_count"),
            "excursions_count": city.get("excursions_count"),
            "tours_count": city.get("tours_count"),
            "guides_count": city.get("guides_count"),
            "review_count_from_top_experiences": sum_int(experiences, "review_count"),
            "min_price": fetch_min_experience_price(db, {"city_id": f"eq.{city_id}"}),
        },
        "popular_topics": common_tags[:12],
        "places_and_attractions": attractions[:12],
        "popular_experiences": experiences[:10],
        "tripster_reference": fetch_tripster_city_reference(city, args),
    }


def fetch_country_context(db: SupabaseRest, country: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    country_id = int(country["id"])
    cities = db.select_all(
        "cities",
        "id,name_ru,name_5_ru,slug,experience_count,tours_count,excursions_count,guides_count,in_obj_phrase",
        filters={"country_id": f"eq.{country_id}"},
        order="experience_count.desc",
        limit=args.context_limit,
    )
    experiences = db.select_all(
        "experiences",
        "title,tagline,city_id,rating,review_count,price_value,price_value_string,price_currency",
        filters={"country_id": f"eq.{country_id}", "status": "eq.active"},
        order="review_count.desc.nullslast",
        limit=args.context_limit,
    )
    city_names = {int(row["id"]): row.get("name_ru") for row in cities if row.get("id") is not None}
    for item in experiences:
        city_id = item.get("city_id")
        item["city_name"] = city_names.get(int(city_id)) if isinstance(city_id, int) else None
    return {
        "location": country,
        "stats": {
            "experience_count": country.get("experience_count"),
            "tours_count": country.get("tours_count"),
            "guides_count": country.get("guides_count"),
            "city_count_in_db": len(cities),
            "review_count_from_top_experiences": sum_int(experiences, "review_count"),
            "min_price": fetch_min_experience_price(db, {"country_id": f"eq.{country_id}"}),
        },
        "popular_cities": cities[:15],
        "popular_experiences": experiences[:12],
    }


def build_messages(entity_type: str, context: dict[str, Any], validation_error: str | None = None) -> list[dict[str, str]]:
    location = context["location"]
    system = (
        "Ты пишешь SEO-блоки для страниц экскурсий по городам и странам. "
        "Пиши на естественном русском языке, как редактор туристического каталога. "
        "Факты бери только из переданного контекста или общеизвестных сведений о направлении; не копируй тексты конкурентов. "
        "Не упоминай Tripster и конкурентов. Не обещай то, чего нет в контексте. "
        "Ответ строго валидный JSON без markdown и пояснений. "
        "JSON должен парситься стандартным json.loads: строки должны быть закрыты, кавычки внутри HTML-атрибутов нужно экранировать или не использовать."
    )
    if entity_type == "city":
        task = {
            "entity_type": "city",
            "location": location,
            "context": context,
            "style_requirements": [
                "Расскажи об основных местах и темах города: история, архитектура, музеи, природа, гастрономия или окрестности, если это есть в контексте.",
                "body должен быть большим расширенным HTML: 10-14 секций с <h2>, <p>, при необходимости <ul><li>.",
                "Ориентир объема body: 1200-1800 слов. Не делай короткий SEO-блок.",
                "В начале body должна быть общая информация об экскурсиях в этом городе, а не сразу рассказ о достопримечательностях.",
                "Первые 1-2 абзаца body без отдельного заголовка: какие экскурсии доступны, сколько маршрутов/гидов есть в контексте, какие форматы встречаются, кому подойдут групповые, индивидуальные, пешие, автобусные или выездные программы, как ориентироваться по цене и отзывам.",
                "Только после общего вводного блока переходи к местам, районам, музеям, природе, гастрономии и окрестностям.",
                "Текст должен помогать выбрать формат экскурсии, а не быть рекламной водой.",
                "Если в context.tripster_reference есть headings, используй их только как подсказку по полноте покрытия тем, но не копируй формулировки и текст.",
                "h1 в форме 'Экскурсии в ...' или другой грамматически корректной форме.",
                "title 50-80 символов, с 2026 и полезным коммерческим сигналом: цены, отзывы или выбор маршрутов.",
                "h2 короткий: что посмотреть или как выбрать экскурсию.",
                "faq: 4-6 вопросов с короткими ответами.",
            ],
        }
    else:
        task = {
            "entity_type": "country",
            "location": location,
            "context": context,
            "style_requirements": [
                "Расскажи, какие города и форматы экскурсий доступны в стране, опираясь на popular_cities и popular_experiences.",
                "body должен быть большим расширенным HTML: 8-12 секций с <h2>, <p>, при необходимости <ul><li>.",
                "Ориентир объема body: 1000-1600 слов. Не делай короткий SEO-блок.",
                "Первый абзац body без отдельного заголовка: коротко о выборе экскурсий по стране.",
                "Не перечисляй города, которых нет в контексте, если не уверен.",
                "h1 в форме 'Экскурсии в ...' или другой грамматически корректной форме.",
                "title 50-80 символов, с 2026 и полезным коммерческим сигналом: города, цены, отзывы или туры.",
                "h2 короткий: как выбрать маршрут по стране.",
                "faq: 4-6 вопросов с короткими ответами.",
            ],
        }
    task["required_output_json"] = {
        "title": "...",
        "h1": "...",
        "h2": "...",
        "body": "<p>...</p><h2>...</h2><p>...</p>",
        "faq": [{"question": "...", "answer": "..."}],
    }
    if validation_error:
        task["previous_error"] = validation_error
        task["fix_instruction"] = (
            "Перегенерируй полный JSON заново. Обязательно верни все ключи: "
            "title, h1, h2, body, faq. Ключ body должен быть непустой HTML-строкой. "
            "Если предыдущий ответ был обрезан или JSON сломан, сделай body короче: 700-1000 слов, но сохрани <p> и <h2>."
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(task, ensure_ascii=False)},
    ]


def generate_valid_fields(
    *,
    entity_type: str,
    context: dict[str, Any],
    api_key: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    validation_error: str | None = None
    for attempt in range(1, args.generation_attempts + 1):
        try:
            fields = call_llm(
                messages=build_messages(entity_type, context, validation_error),
                api_key=api_key,
                api_base=args.api_base,
                model=args.model,
                max_output_tokens=args.max_output_tokens,
                response_format=not args.no_response_format,
                timeout=args.timeout,
                max_retries=args.max_retries,
            )
            return validate_output(fields)
        except (RuntimeError, json.JSONDecodeError) as exc:
            validation_error = str(exc)
            if attempt == args.generation_attempts:
                raise
            print(
                f"retry {attempt}/{args.generation_attempts} {entity_type}={context['location']['id']}: {validation_error}",
                flush=True,
            )
    raise RuntimeError("unreachable")


def target_label(entity_type: str, row: dict[str, Any]) -> str:
    return f"{entity_type}:{row['id']}:{row.get('name_ru') or row.get('name_en')}"


def should_skip(existing: dict[str, Any] | None, args: argparse.Namespace) -> bool:
    return bool(existing and existing.get("body") and not args.force)


def write_seo_block(
    db: SupabaseRest,
    *,
    entity_type: str,
    entity_id: int,
    values: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    existing = fetch_existing_block(db, entity_type, entity_id)
    payload = {
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "slug": location_slug(entity_type, entity_id),
        "h1": values["h1"],
        "title": values["title"],
        "h2": values["h2"],
        "body": values["body"],
        "faq": faq_for_storage(values["faq"]),
        "is_active": True,
    }
    if args.dry_run:
        return {"operation": "update" if existing else "insert", **payload}
    if existing:
        db.patch_by_id("seo_blocks", int(existing["id"]), payload)
        return {"operation": "updated", "id": existing["id"], **payload}
    inserted = db.insert("seo_blocks", payload)
    return {"operation": "inserted", "id": inserted.get("id"), **payload}


def process_target(
    *,
    db: SupabaseRest,
    target: dict[str, Any],
    api_key: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    entity_type = target["entity_type"]
    row = target["row"]
    entity_id = int(row["id"])
    existing = fetch_existing_block(db, entity_type, entity_id)
    if should_skip(existing, args):
        return {"operation": "skipped", "entity_type": entity_type, "entity_id": entity_id, "name": row.get("name_ru")}

    context = fetch_city_context(db, row, args) if entity_type == "city" else fetch_country_context(db, row, args)
    fields = generate_valid_fields(entity_type=entity_type, context=context, api_key=api_key, args=args)
    result = write_seo_block(db, entity_type=entity_type, entity_id=entity_id, values=fields, args=args)
    return {"name": row.get("name_ru"), **result}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Populate city/country SEO blocks in Supabase seo_blocks.")
    parser.add_argument("--entity", choices=["city", "country", "both"], default="city", help="Entity type to process. Default: city.")
    parser.add_argument("--limit", type=int, default=1, help="Max rows per entity type. Default: 1.")
    parser.add_argument("--city-id", type=int, action="append", help="City id to process. Can be repeated.")
    parser.add_argument("--city-slug", action="append", help="City slug to process. Can be repeated.")
    parser.add_argument("--country-id", type=int, action="append", help="Country id to process. Can be repeated.")
    parser.add_argument("-force", "--force", "--all", action="store_true", help="Regenerate selected blocks even when body is already filled.")
    parser.add_argument("--dry-run", action="store_true", help="Generate and print result without writing to Supabase.")
    parser.add_argument("--context-limit", type=int, default=30, help="Max tags/experiences/cities used as LLM context. Default: 30.")
    parser.add_argument("--no-tripster-reference", action="store_true", help="Do not fetch Tripster city SEO outline even when TRIPSTER_TOKEN is set.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS, help="Max output tokens for LLM response. Default: 20000.")
    parser.add_argument("--no-response-format", action="store_true", help="Do not send OpenAI-compatible response_format=json_object.")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--generation-attempts", type=int, default=3, help="LLM regeneration attempts per row.")
    parser.add_argument("--workers", type=int, default=4, help="Parallel entity rows to process. Default: 4.")
    args = parser.parse_args()
    if args.limit < 1:
        raise RuntimeError("--limit must be >= 1")
    if args.context_limit < 5:
        raise RuntimeError("--context-limit must be >= 5")
    if args.generation_attempts < 1:
        raise RuntimeError("--generation-attempts must be >= 1")
    if args.max_output_tokens < 1000:
        raise RuntimeError("--max-output-tokens must be >= 1000")
    if args.workers < 1:
        raise RuntimeError("--workers must be >= 1")
    if args.city_id and args.city_slug:
        raise RuntimeError("Use either --city-id or --city-slug, not both")
    return args


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("Set LLM_API_KEY in .env")

    db = supabase_from_env(args.timeout, args.max_retries)
    targets: list[dict[str, Any]] = []
    if args.entity in {"city", "both"}:
        targets.extend(fetch_city_targets(db, args))
    if args.entity in {"country", "both"}:
        targets.extend(fetch_country_targets(db, args))
    if not targets:
        print("No location rows selected", flush=True)
        return

    print(f"selected={len(targets)}, model={args.model}, dry_run={args.dry_run}, workers={args.workers}", flush=True)
    for target in targets:
        print(f"target {target_label(target['entity_type'], target['row'])}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_targets = {
            executor.submit(process_target, db=db, target=target, api_key=api_key, args=args): target
            for target in targets
        }
        errors: list[dict[str, str]] = []
        for index, future in enumerate(as_completed(future_targets), start=1):
            target = future_targets[future]
            try:
                result = future.result()
            except Exception as exc:
                row = target["row"]
                error = {
                    "entity_type": target["entity_type"],
                    "entity_id": str(row.get("id")),
                    "name": str(row.get("name_ru") or row.get("name_en") or ""),
                    "error": str(exc),
                }
                errors.append(error)
                print(
                    f"failed {index}/{len(targets)} {error['entity_type']}={error['entity_id']} "
                    f"{error['name']!r}: {error['error']}",
                    flush=True,
                )
                continue
            if args.dry_run:
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            else:
                print(
                    f"{result['operation']} {index}/{len(targets)} "
                    f"{result.get('entity_type')}={result.get('entity_id')} {result.get('name')!r}",
                    flush=True,
                )
        if errors:
            print(f"errors={len(errors)}", flush=True)
            for error in errors:
                print(
                    f"error {error['entity_type']}={error['entity_id']} {error['name']!r}: {error['error']}",
                    flush=True,
                )


if __name__ == "__main__":
    main()
