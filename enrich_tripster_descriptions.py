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


TOKENATOR_URL = "https://api.tokenator.top/v1/chat/completions"
CODA_URL = "https://api.coda.ink/v1/chat/completions"
APICLAUDE_URL = "https://apiclaude.ooguy.com/v1/chat/completions"
DEFAULT_TOKENATOR_MODEL = "gemini-3.5-flash"
DEFAULT_CODA_MODEL = "claude-sonnet-4.6"
DEFAULT_APICLAUDE_MODEL = "claude-opus-4-8"
DEFAULT_WORKERS = 5
REST_PAGE_SIZE = 1000
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
        adapter = requests.adapters.HTTPAdapter(pool_connections=30, pool_maxsize=30)
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
                raise requests.HTTPError(f"{exc}; body={response.text[:1000]}", response=response) from exc
            return response

        if attempt == 5:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else min(30.0, 2 ** (attempt - 1) + random.random())
        time.sleep(delay)

    raise RuntimeError("request failed")


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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

    def select_experiences(
        self,
        *,
        only_missing: bool,
        max_rows: int | None,
        city_ids: set[int] | None,
        experience_ids: set[int] | None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        last_id = 0
        while True:
            limit = REST_PAGE_SIZE
            if max_rows is not None:
                limit = min(limit, max_rows - len(rows))
                if limit <= 0:
                    return rows

            params: dict[str, Any] = {
                "select": (
                    "id,title,description_html,meeting_point_text,finish_point_text,"
                    "price_included_description,price_not_included_description"
                ),
                "id": f"gt.{last_id}",
                "description_html": "not.is.null",
                "order": "id.asc",
                "limit": limit,
            }
            if only_missing:
                params["description_new_html"] = "is.null"
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
            rows.extend(page)
            if len(page) < limit or experience_ids:
                return rows
            last_id = int(page[-1]["id"])

    def update_description(self, experience_id: int, description_new_html: str) -> None:
        request_with_retries(
            "PATCH",
            f"{self.base_url}/experiences",
            headers={**self.headers, "Prefer": "return=minimal"},
            params={"id": f"eq.{experience_id}"},
            json={
                "description_new_html": description_new_html,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            timeout=self.timeout,
        )


def compact_extra_payload(item: dict[str, Any]) -> str:
    parts: list[str] = []
    if item.get("meeting_point_text"):
        parts.append(f"Место сбора из базы: {item['meeting_point_text']}")
    if item.get("finish_point_text"):
        parts.append(f"Место окончания из базы: {item['finish_point_text']}")
    if item.get("price_included_description"):
        parts.append(f"Что включено из базы: {item['price_included_description']}")
    if item.get("price_not_included_description"):
        parts.append(f"Что не включено из базы: {item['price_not_included_description']}")
    return "\n\n".join(parts)


def build_prompt(item: dict[str, Any]) -> str:
    extra = compact_extra_payload(item)
    extra_block = f"\n\nДополнительные структурные поля из базы:\n{extra}" if extra else ""
    return f"""Ты редактор карточек экскурсий.

Нужно переписать описание экскурсии в понятную, структурированную HTML-версию.
Используй только факты из исходного описания и дополнительных полей. Если для раздела нет реального контента, не придумывай и не натягивай.

Требования к ответу:
- Верни только HTML-фрагмент, без Markdown, без ```html и без пояснений.
- Используй заголовки <h2>.
- Допустимые теги: <h2>, <p>, <ul>, <li>, <strong>, <br>.
- Не добавляй ссылки, скрипты, стили, классы и id.
- Сохрани цены, условия и ограничения, если они есть.
- FAQ сделай 3-5 пар вопрос-ответ только по информации из текста.

Разделы:
<h2>Мини-описание</h2>
<h2>Кому подойдет</h2>
<h2>Что увидим</h2>
<h2>Маршрут/программа</h2>
<h2>Что включено</h2>
<h2>Что не включено / доп. расходы / опции с ценами</h2>
<h2>Физическая нагрузка</h2>
<h2>Что взять с собой</h2>
<h2>Место сбора</h2>
<h2>FAQ</h2>

Название экскурсии:
{item.get("title") or ""}

Исходное описание:
{item.get("description_html") or ""}{extra_block}
"""


def strip_code_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def extract_message_content(payload: dict[str, Any], provider: str) -> str:
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices or not isinstance(choices, list):
        raise RuntimeError(f"Unexpected {provider} response: {payload!r}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not content:
        raise RuntimeError(f"{provider} returned empty content: {payload!r}")
    return strip_code_fence(str(content))


def generate_with_tokenator(item: dict[str, Any], api_key: str, model: str, timeout: float) -> str:
    response = request_with_retries(
        "POST",
        TOKENATOR_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": build_prompt(item)}],
            "reasoning": {"enabled": True},
        },
        timeout=timeout,
    )
    return extract_message_content(response.json(), "Tokenator")


def generate_with_apiclaude(item: dict[str, Any], api_key: str, model: str, timeout: float) -> str:
    response = request_with_retries(
        "POST",
        APICLAUDE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": build_prompt(item)}],
        },
        timeout=timeout,
    )
    return extract_message_content(response.json(), "ApiClaude")


def extract_stream_delta(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error
        raise RuntimeError(f"Coda error: {message}")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""

    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))

    message = choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(choice.get("text"), str):
        return choice["text"]
    return ""


def generate_with_coda(item: dict[str, Any], api_key: str, model: str, timeout: float) -> str:
    response = request_with_retries(
        "POST",
        CODA_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "stream": True,
            "messages": [{"role": "user", "content": build_prompt(item)}],
        },
        stream=True,
        timeout=timeout,
    )

    chunks: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        chunks.append(extract_stream_delta(payload))

    content = "".join(chunks).strip()
    if not content:
        raise RuntimeError("Coda returned empty streamed content")
    return strip_code_fence(content)


def generate_description(item: dict[str, Any], provider: str, api_key: str, model: str, timeout: float) -> str:
    if provider == "tokenator":
        return generate_with_tokenator(item, api_key, model, timeout)
    if provider == "coda":
        return generate_with_coda(item, api_key, model, timeout)
    if provider == "apiclaude":
        return generate_with_apiclaude(item, api_key, model, timeout)
    raise RuntimeError(f"Unsupported provider: {provider}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate structured HTML descriptions.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--provider", choices=["tokenator", "coda", "apiclaude"], default="tokenator")
    parser.add_argument("--model")
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--id", action="append", help="One experience id or comma-separated ids.")
    parser.add_argument("--city-id", action="append", help="One city id or comma-separated city id list.")
    parser.add_argument("--city-slug", action="append", help="One city slug or comma-separated slug list.")
    parser.add_argument("--all", action="store_true", help="Regenerate rows that already have description_new_html.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def parse_int_set(values: list[str] | None) -> set[int] | None:
    if not values:
        return None
    return {int(part.strip()) for value in values for part in value.split(",") if part.strip()}


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env")

    model = args.model
    if args.provider == "tokenator":
        api_key = os.getenv("TOKENATOR_API_KEY")
        model = model or DEFAULT_TOKENATOR_MODEL
        if not api_key:
            raise SystemExit("Set TOKENATOR_API_KEY in .env")
    elif args.provider == "coda":
        api_key = os.getenv("CODA_API_KEY")
        model = model or DEFAULT_CODA_MODEL
        if not api_key:
            raise SystemExit("Set CODA_API_KEY in .env")
    else:
        api_key = os.getenv("APICLAUDE_API_KEY")
        model = model or DEFAULT_APICLAUDE_MODEL
        if not api_key:
            raise SystemExit("Set APICLAUDE_API_KEY in .env")

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
    experiences = db.select_experiences(
        only_missing=not args.all,
        max_rows=args.max_rows,
        city_ids=selected_city_ids,
        experience_ids=selected_experience_ids,
    )
    if not experiences:
        log("No experiences selected")
        return

    mode = "DRY RUN" if args.dry_run else "IMPORT"
    log(f"{mode}: rows={len(experiences)}, workers={args.workers}, provider={args.provider}, model={model}")

    imported = errors = 0
    started_at = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(generate_description, item, args.provider, api_key, model, args.timeout): item
            for item in experiences
        }
        for done, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            experience_id = int(item["id"])
            try:
                description_new_html = future.result()
                if not args.dry_run:
                    db.update_description(experience_id, description_new_html)
                imported += 1
                log(f"[{done}/{len(experiences)}] {experience_id}: OK")
            except Exception as exc:
                errors += 1
                log(f"[{done}/{len(experiences)}] {experience_id}: ERROR: {exc}")
                continue

            if done == 1 or done % 25 == 0 or done == len(experiences):
                log(f"[{done}/{len(experiences)}] saved={imported}, errors={errors}")

    elapsed_seconds = max(time.monotonic() - started_at, 0.001)
    rows_per_minute = imported / elapsed_seconds * 60
    seconds_per_row = elapsed_seconds / imported if imported else 0
    log(
        f"Done: imported={imported}, errors={errors}, "
        f"elapsed={elapsed_seconds:.1f}s, speed={rows_per_minute:.2f}/min, avg={seconds_per_row:.2f}s/row"
    )


if __name__ == "__main__":
    main()
