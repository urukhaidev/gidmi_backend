from __future__ import annotations

import argparse
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


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REST_PAGE_SIZE = 1000
DEFAULT_API_BASE = "https://api.aigate.shop/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
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
        parsed = json.loads(value, strict=False)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder(strict=False)
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


def category_name(row: dict[str, Any]) -> str:
    sub_name = str(row.get("sub_name") or "").strip()
    main_name = str(row.get("main_name") or "").strip()
    return sub_name or main_name or str(row.get("sub_slug") or "").strip()


def build_messages(row: dict[str, Any], validation_error: str | None = None) -> list[dict[str, str]]:
    category = category_name(row)
    system = (
        "Ты пишешь programmatic SEO-шаблоны для страниц категорий экскурсий. "
        "Пиши на естественном русском языке, без выдуманных фактов о городе и без конкретных достопримечательностей. "
        "Плейсхолдеры вида {{city}} нужно оставлять без замены. "
        "Не ставь перед {{city}} предлоги и служебные слова вроде «в», «во», «по», «из», «для»: нужная форма города уже хранится в базе. "
        "Ответ строго JSON без markdown и пояснений."
    )
    user = {
        "category": category,
        "catalog_row": {
            "id": row.get("id"),
            "main_name": row.get("main_name"),
            "sub_name": row.get("sub_name"),
            "main_slug": row.get("main_slug"),
            "sub_slug": row.get("sub_slug"),
        },
        "placeholders_do_not_replace": [
            "{{city}} — название города",
            "{{count}} — количество экскурсий",
            "{{reviews}} — количество отзывов",
            "{{months}} — текущий сезон или месяцы",
            "{{min_price}} — минимальная цена",
        ],
        "fields": {
            "header": "H1 для категории. Пример формы: Групповые экскурсии {{city}}. Адаптируй к category.",
            "title": "SEO title для категории: 50-70 символов, максимум 80. Используй {{city}}, {{months}} перед 2026, цены или отзывы. Пример формы: Экскурсии на автомобиле {{city}} {{months}} 2026, от {{min_price}}. Адаптируй к category.",
            "seo_description": "Meta description 140-180 символов, используй {{city}} без предлога перед ним, добавь пользу для пользователя, цены/отзывы или выбор экскурсий.",
            "seo_text": (
                "Шаблон SEO-текста для категории category. Сделай текст чуть подробнее: 3 абзаца, желательно от 150 слов, без заголовков. "
                "Первый абзац — преимущества экскурсий category {{city}}. "
                "Второй — форматы экскурсий, индивидуальные и групповые варианты, удобство выбора и цену от {{min_price}}. "
                "Третий — упоминает {{count}} экскурсий, {{reviews}} отзывов и сезон {{months}}. "
                "Текст должен быть универсальным для любого города. Не используй конкретные достопримечательности или факты о городе. "
                "Не пиши шаблонными фразами вроде «отличная возможность»."
            ),
        },
        "required_output_json": {
            "title": "...",
            "header": "...",
            "seo_description": "...",
            "seo_text": "3 абзаца через пустую строку",
        },
    }
    if validation_error:
        user["previous_error"] = validation_error
        user["fix_instruction"] = "Перегенерируй все поля так, чтобы ошибка была исправлена."
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def call_llm(
    *,
    messages: list[dict[str, str]],
    api_key: str,
    api_base: str,
    model: str,
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    response = request_with_retries(
        "POST",
        f"{api_base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.4},
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


def clean_field(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"Expected string field, got {value!r}")
    return value.strip().replace("\r\n", "\n")


def validate_seo_fields(fields: dict[str, Any], row: dict[str, Any]) -> dict[str, str]:
    return {
        "title": clean_field(fields.get("title")),
        "header": clean_field(fields.get("header")),
        "seo_description": clean_field(fields.get("seo_description")),
        "seo_text": clean_field(fields.get("seo_text")),
    }


def generate_valid_fields(
    *,
    row: dict[str, Any],
    api_key: str,
    args: argparse.Namespace,
) -> dict[str, str]:
    validation_error: str | None = None
    for attempt in range(1, args.generation_attempts + 1):
        try:
            fields = call_llm(
                messages=build_messages(row, validation_error),
                api_key=api_key,
                api_base=args.api_base,
                model=args.model,
                timeout=args.timeout,
                max_retries=args.max_retries,
            )
            return validate_seo_fields(fields, row)
        except Exception as exc:
            validation_error = str(exc)
            if attempt == args.generation_attempts:
                raise
            print(
                f"retry {attempt}/{args.generation_attempts} id={row['id']}: {validation_error}",
                flush=True,
            )
    raise RuntimeError("unreachable")


def fetch_catalog_rows(db: SupabaseRest, args: argparse.Namespace) -> list[dict[str, Any]]:
    filters: dict[str, str] = {}
    if args.catalog_id:
        filters["id"] = f"in.({','.join(str(item) for item in args.catalog_id)})"
    elif args.sub_slug:
        filters["sub_slug"] = f"in.({','.join(args.sub_slug)})"
    elif not args.force:
        filters["seo_text"] = "is.null"

    return db.select_all(
        "tag_category_catalog",
        "id,main_slug,main_name,sub_slug,sub_name,title,header,seo_description,seo_text",
        filters=filters or None,
        order="id.asc",
        limit=args.limit,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Populate tag_category_catalog SEO fields with AIGate LLM templates.")
    parser.add_argument("--limit", type=int, default=1, help="Max catalog rows to process. Default: 1.")
    parser.add_argument("--catalog-id", type=int, action="append", help="Catalog id to process. Can be repeated.")
    parser.add_argument("--sub-slug", action="append", help="Catalog sub_slug to process. Can be repeated.")
    parser.add_argument("-force", "--force", "--all", action="store_true", help="Process selected rows even when seo_text is already filled.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Supabase.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--generation-attempts", type=int, default=2, help="LLM regeneration attempts per row.")
    parser.add_argument("--workers", type=int, default=10, help="Parallel catalog rows to process. Default: 10.")
    args = parser.parse_args()
    if args.limit < 1:
        raise RuntimeError("--limit must be >= 1")
    if args.generation_attempts < 1:
        raise RuntimeError("--generation-attempts must be >= 1")
    if args.workers < 1:
        raise RuntimeError("--workers must be >= 1")
    return args


def process_row(
    *,
    db: SupabaseRest,
    row: dict[str, Any],
    api_key: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    update = generate_valid_fields(row=row, api_key=api_key, args=args)
    if not args.dry_run:
        db.patch_by_id("tag_category_catalog", int(row["id"]), update)
    return {"ok": True, "id": row["id"], "category": category_name(row), **update}


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("Set LLM_API_KEY in .env")

    db = supabase_from_env(args.timeout, args.max_retries)
    rows = fetch_catalog_rows(db, args)
    if not rows:
        print("No catalog rows selected", flush=True)
        return

    print(f"selected={len(rows)}, model={args.model}, dry_run={args.dry_run}, workers={args.workers}", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_row, db=db, row=row, api_key=api_key, args=args): row
            for row in rows
        }
        failed = 0
        for index, future in enumerate(as_completed(futures), start=1):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                failed += 1
                print(
                    f"failed {index}/{len(rows)} id={row['id']} category={category_name(row)!r}: {exc}",
                    flush=True,
                )
                continue
            if args.dry_run:
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            else:
                print(
                    f"updated {index}/{len(rows)} id={result['id']} category={result['category']!r}",
                    flush=True,
                )
        if failed:
            print(f"Done with errors: failed={failed}, succeeded={len(rows) - failed}", flush=True)


if __name__ == "__main__":
    main()
