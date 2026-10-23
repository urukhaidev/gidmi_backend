from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

import tag_category_linker as linker


PROJECT_BATCH_SIZE = 60
UPDATE_WORKERS = 12


def build_messages(batch: list[dict[str, object]]) -> list[dict[str, str]]:
    system = (
        "Ты готовишь строгий лексический reranker для категорий экскурсий. "
        "Для каждой категории верни 3–8 коротких русских подстрок или корней, "
        "которые прямо подтверждают её наличие в тексте экскурсии. "
        "Используй основы слов, например «лошад», «конн», «верхов», а не все словоформы. "
        "Не добавляй общие туристические слова. Если категория субъективная или не может "
        "быть надёжно подтверждена буквальным текстом, верни пустой массив. "
        "Ответ строго JSON: {\"items\":[{\"id\":число,\"triggers\":[\"...\"]}]}"
    )
    user = {
        "categories": [
            {"id": row["id"], "category": f"{row['main_name']} / {row['sub_name']}"}
            for row in batch
        ]
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def normalize_triggers(values: object) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"Invalid triggers: {values!r}")
    result: list[str] = []
    for value in values:
        value = value.strip().lower()
        if value and value not in result:
            result.append(value)
    return result


def update_row(db: linker.SupabaseRest, row: dict[str, object]) -> None:
    linker.request_with_retries(
        "PATCH",
        f"{db.base_url}/tag_category_catalog",
        headers={**db.headers, "Prefer": "return=minimal"},
        params={"id": f"eq.{row['id']}"},
        json={"lexical_triggers": row["lexical_triggers"]},
        timeout=linker.DEFAULT_TIMEOUT,
    )


def main() -> None:
    linker.load_dotenv(Path(__file__).resolve().with_name(".env"))
    db = linker.supabase_from_env()
    catalog = db.select_all("tag_category_catalog", "id,main_name,sub_name", order="id.asc")
    settings = linker.llm_settings(SimpleNamespace(model=None))
    updates: list[dict[str, object]] = []

    for start in range(0, len(catalog), PROJECT_BATCH_SIZE):
        batch = catalog[start : start + PROJECT_BATCH_SIZE]
        proposal = linker.request_llm(settings, build_messages(batch), 120.0, 3)
        expected_ids = {int(row["id"]) for row in batch}
        items = proposal.get("items", [])
        found_ids = {int(item["id"]) for item in items if str(item.get("id", "")).isdigit()}
        if found_ids != expected_ids:
            raise RuntimeError(f"Incomplete LLM response for batch {start // PROJECT_BATCH_SIZE + 1}")
        updates.extend(
            {"id": int(item["id"]), "lexical_triggers": normalize_triggers(item.get("triggers"))}
            for item in items
        )
        print(f"Prepared {min(start + PROJECT_BATCH_SIZE, len(catalog))}/{len(catalog)}", flush=True)

    with ThreadPoolExecutor(max_workers=UPDATE_WORKERS) as executor:
        futures = [executor.submit(update_row, db, row) for row in updates]
        for completed, future in enumerate(as_completed(futures), start=1):
            future.result()
            if completed % 50 == 0 or completed == len(updates):
                print(f"Updated {completed}/{len(updates)}", flush=True)

    print(
        json.dumps(
            {
                "updated": len(updates),
                "nonempty": sum(bool(row["lexical_triggers"]) for row in updates),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
