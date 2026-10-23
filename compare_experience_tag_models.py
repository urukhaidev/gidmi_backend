from __future__ import annotations

import argparse
import json
import os
import random
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from tag_category_linker import DEFAULT_EMBEDDING_MODEL, pgvector_literal, request_embeddings
from tag_experience_classifier import (
    DEFAULT_API_BASE,
    attach_tripster_common_tags,
    build_messages,
    call_llm,
    fetch_allowed_catalog,
    load_dotenv,
    log,
    result_rows,
    split_csv_tags,
    supabase_from_env,
)


DEFAULT_SAMPLE_SIZE = 1
MODES = ("cache", "no-cache", "no-commons", "embedding-no-commons", "embedding-commons", "reasoning")
DEFAULT_MODELS = (
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-4.8",
)
EXTRA_MODELS = (
    "openai/gpt-5.4-mini",
    "openai/gpt-5.5",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "google/gemini-3.1-pro-preview",
    "z-ai/glm-5.2",
)
LEXICAL_MODEL = "lexical_triggers"
LEXICAL_MODE = "lexical"
EMBEDDING_TOP_K = 120


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare tag classification results across models and modes. "
            "The script is read-only and samples one experience by default."
        )
    )
    parser.add_argument(
        "--model",
        action="append",
        help="Model to test; repeat the option to replace the default Claude model set.",
    )
    parser.add_argument(
        "--include-extra-models",
        action="store_true",
        help="Add OpenAI, DeepSeek, Gemini and GLM models to the default Claude model set.",
    )
    parser.add_argument(
        "--mode",
        action="append",
        choices=MODES,
        help="Mode to test; repeat it. Default: no-cache, no-commons, embedding-no-commons, embedding-commons.",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_SAMPLE_SIZE, help="Number of random experiences, default 1.")
    parser.add_argument("--seed", type=int, help="Seed for reproducible random sampling.")
    parser.add_argument("--experience-id", action="append", help="Optional id or comma-separated ids instead of random selection.")
    parser.add_argument("--api-base", default=os.environ.get("LLM_API_BASE") or DEFAULT_API_BASE)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--workers", type=int, default=150, help="Maximum concurrent LLM requests, default 150.")
    parser.add_argument(
        "--html-out",
        default="compare_experience_tag_models_report.html",
        help="Path for the interactive HTML report.",
    )
    parser.add_argument("--no-open", action="store_true", help="Write the HTML report but do not open it in a browser.")
    return parser.parse_args()


def parse_ids(values: list[str] | None) -> list[int] | None:
    if not values:
        return None
    return [int(part.strip()) for value in values for part in value.split(",") if part.strip()]


def fetch_random_experiences(db: Any, count: int, experience_ids: list[int] | None, rng: random.Random) -> list[dict[str, Any]]:
    if experience_ids:
        rows = db.select_all(
            "experiences", "id,title,tagline,annotation", filters={"id": f"in.({','.join(map(str, experience_ids))})"}, order="id.asc"
        )
        if len(rows) != len(set(experience_ids)):
            found = {int(row["id"]) for row in rows}
            raise RuntimeError(f"Some --experience-id values were not found: {sorted(set(experience_ids) - found)}")
        return rows

    # Do not load the whole experiences table. Sampling by numeric id is fast and sufficiently
    # random for a manual model comparison; gaps are handled by taking the next existing row.
    first = db.select_first("experiences", "id", filters={"title": "not.is.null"}, order="id.asc")
    last = db.select_first("experiences", "id", filters={"title": "not.is.null"}, order="id.desc")
    if not first or not last:
        raise RuntimeError("No experiences with a title are available.")
    min_id, max_id = int(first["id"]), int(last["id"])
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    max_attempts = max(count * 10, 20)
    for _ in range(max_attempts):
        if len(selected) >= count:
            break
        candidate_id = rng.randint(min_id, max_id)
        row = db.select_first(
            "experiences",
            "id,title,tagline,annotation",
            filters={"id": f"gte.{candidate_id}", "title": "not.is.null"},
            order="id.asc",
        )
        if row is None:
            row = db.select_first(
                "experiences",
                "id,title,tagline,annotation",
                filters={"id": f"lte.{candidate_id}", "title": "not.is.null"},
                order="id.desc",
            )
        if row and int(row["id"]) not in selected_ids:
            selected.append(row)
            selected_ids.add(int(row["id"]))
    if len(selected) < count:
        raise RuntimeError(f"Could only select {len(selected)} unique experiences; requested {count}.")
    return selected


def usage_cost(usage: Any) -> str:
    if not isinstance(usage, dict):
        return "—"
    value = usage.get("cost_usd", usage.get("total_cost", usage.get("cost")))
    if value is None or value == "":
        return "—"
    try:
        return f"${float(value):.6f}"
    except (TypeError, ValueError):
        return str(value)


def parse_lexical_triggers(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
            values = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            values = []
    else:
        values = []
    return [str(item).strip().lower() for item in values if str(item).strip()]


def fetch_lexical_catalog(db: Any) -> list[tuple[str, list[str]]]:
    rows = db.select_all(
        "tag_category_catalog",
        "sub_slug,lexical_triggers",
        filters={"is_common": "eq.true"},
        order="sub_slug.asc",
    )
    triggers_by_sub_slug: dict[str, list[str]] = {}
    for row in rows:
        sub_slug = str(row.get("sub_slug") or "").strip()
        triggers = parse_lexical_triggers(row.get("lexical_triggers"))
        if not sub_slug or not triggers:
            continue
        target = triggers_by_sub_slug.setdefault(sub_slug, [])
        for trigger in triggers:
            if trigger not in target:
                target.append(trigger)
    return sorted(triggers_by_sub_slug.items())


def lexical_tags(experience: dict[str, Any], catalog: list[tuple[str, list[str]]]) -> list[str]:
    text = f"{experience.get('title') or ''}\n{experience.get('tagline') or ''}\n{experience.get('annotation') or ''}".lower().replace("ё", "е")
    matches: list[str] = []
    for sub_slug, triggers in catalog:
        if any(trigger.replace("ё", "е") in text for trigger in triggers):
            matches.append(sub_slug)
    return matches


def embedding_settings() -> tuple[str, str, str]:
    api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_FALLBACK_API_KEY")
    api_base = os.environ.get("EMBEDDING_API_BASE") or os.environ.get("LLM_FALLBACK_API_BASE") or "https://openrouter.ai/api/v1"
    model = os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
    if not api_key:
        raise RuntimeError("Embedding modes require EMBEDDING_API_KEY or LLM_FALLBACK_API_KEY in .env")
    return api_key, api_base, model


def embedding_catalog_for_experience(db: Any, experience: dict[str, Any], timeout: float, max_retries: int) -> list[str]:
    settings = embedding_settings()
    content = f"{experience.get('title') or ''}\n{experience.get('tagline') or ''}\n{experience.get('annotation') or ''}"
    embedding = request_embeddings(settings, [content], timeout, max_retries)[0]
    rows = db.rpc(
        "match_tag_category_catalog_embeddings",
        {
            "p_embedding": pgvector_literal(embedding),
            "p_model": settings[2],
            "p_limit": EMBEDDING_TOP_K,
        },
    )
    seen: set[str] = set()
    return [
        sub_slug
        for row in rows
        if (sub_slug := str(row.get("sub_slug") or "").strip()) and not (sub_slug in seen or seen.add(sub_slug))
    ]


def run_request(
    model: str,
    mode: str,
    experience: dict[str, Any],
    allowed_sub_slugs: list[str],
    api_key: str,
    args: argparse.Namespace,
 ) -> tuple[str, str, int, dict[str, Any]]:
    use_cache = mode == "cache"
    use_reasoning = mode == "reasoning"
    include_tripster_common_tags = mode not in {"no-commons", "embedding-no-commons"}
    allowed_set = set(allowed_sub_slugs)
    messages = build_messages(
        [experience],
        allowed_sub_slugs,
        use_prompt_cache=use_cache,
        allow_new_and_places=False,
        include_tripster_common_tags=include_tripster_common_tags,
    )
    request_body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
    if use_reasoning:
        request_body["reasoning"] = {"enabled": True, "effort": "high"}
    try:
        proposal = call_llm(
            messages=messages,
            api_key=api_key,
            api_base=args.api_base,
            model=model,
            timeout=args.timeout,
            max_retries=args.max_retries,
            use_reasoning=use_reasoning,
            force_openai_reasoning=True,
        )
        parsed = result_rows(proposal, {int(experience["id"]): experience}, allowed_set, allow_new_and_places=False)[0]
        row = {
            "tags": split_csv_tags(parsed["tags"]),
            "cost": usage_cost(proposal.get("_usage")),
            "error": "",
            "request_body": request_body,
        }
    except Exception as exc:  # Keep other models and the final comparison usable.
        row = {"tags": [], "cost": "—", "error": str(exc), "request_body": request_body}
    return model, mode, int(experience["id"]), row


def add_lexical_results(
    variants: dict[tuple[str, str], dict[int, dict[str, Any]]],
    experiences: list[dict[str, Any]],
    catalog: list[tuple[str, list[str]]],
) -> None:
    variants[(LEXICAL_MODEL, LEXICAL_MODE)] = {
        int(experience["id"]): {
            "tags": lexical_tags(experience, catalog),
            "cost": "—",
            "error": "",
            "request_body": None,
        }
        for experience in experiences
    }


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_table(experiences: list[dict[str, Any]], variants: dict[tuple[str, str], dict[int, dict[str, Any]]]) -> str:
    header = "| ID | Экскурсия | Старые common-теги Tripster | Кол-во старых | Модель / режим | Теги | Кол-во | Стоимость | Общие теги всех моделей | Кол-во общих |"
    separator = "|---:|---|---|---:|---|---|---:|---:|---|---:|"
    lines = [header, separator]
    for experience in experiences:
        experience_id = int(experience["id"])
        # A model may be checked in several modes. Combine its successful mode results first,
        # then intersect the model-level sets: the reported value is common across models,
        # not artificially narrowed by cache/reasoning variations of one model.
        tags_by_model: dict[str, set[str]] = {}
        for (model, _mode), result in variants.items():
            current = result[experience_id]
            if current["error"]:
                continue
            tags_by_model.setdefault(model, set()).update(current["tags"])
        all_models = {model for model, _mode in variants}
        tag_sets = list(tags_by_model.values()) if set(tags_by_model) == all_models else []
        common_tags = sorted(set.intersection(*tag_sets)) if tag_sets else []
        common_value = ", ".join(common_tags) or "—"
        old_tripster_tags = [str(tag.get("slug") or tag.get("name") or "") for tag in experience.get("tripster_common_tags") or []]
        old_tripster_tags = [tag for tag in old_tripster_tags if tag]
        for (model, mode), result in variants.items():
            current = result[experience_id]
            tags = ", ".join(current["tags"]) or "—"
            if current["error"]:
                tags = f"ОШИБКА: {current['error'][:160]}"
            lines.append(
                "| {id} | {title} | {old_tags} | {old_count} | {variant} | {tags} | {count} | {cost} | {common} | {common_count} |".format(
                    id=experience_id,
                    title=markdown_cell(str(experience.get("title") or "")),
                    old_tags=markdown_cell(", ".join(old_tripster_tags) or "—"),
                    old_count=len(old_tripster_tags),
                    variant=markdown_cell(f"{model} / {mode}"),
                    tags=markdown_cell(tags),
                    count=len(current["tags"]),
                    cost=current["cost"],
                    common=markdown_cell(common_value),
                    common_count=len(common_tags),
                )
            )
    return "\n".join(lines)


def report_data(experiences: list[dict[str, Any]], variants: dict[tuple[str, str], dict[int, dict[str, Any]]]) -> dict[str, Any]:
    models = list(dict.fromkeys(model for model, _mode in variants))
    modes = list(dict.fromkeys(mode for _model, mode in variants if mode != LEXICAL_MODE))
    return {
        "models": models,
        "modes": modes,
        "experiences": [
            {
                "id": int(experience["id"]),
                "title": str(experience.get("title") or ""),
                "annotation": str(experience.get("annotation") or ""),
                "all_tripster_tags_count": int(experience.get("tripster_all_tags_count") or 0),
                "old_tripster_tags": [
                    str(tag.get("slug") or tag.get("name") or "")
                    for tag in experience.get("tripster_common_tags") or []
                    if tag.get("slug") or tag.get("name")
                ],
                "results": [
                    {
                        "model": model,
                        "mode": mode,
                        "tags": result[int(experience["id"])]["tags"],
                        "cost": result[int(experience["id"])]["cost"],
                        "error": result[int(experience["id"])]["error"],
                        "request_body": result[int(experience["id"])]["request_body"],
                    }
                    for (model, mode), result in variants.items()
                ],
            }
            for experience in experiences
        ],
    }


def render_html_report(experiences: list[dict[str, Any]], variants: dict[tuple[str, str], dict[int, dict[str, Any]]]) -> str:
    data = json.dumps(report_data(experiences, variants), ensure_ascii=False).replace("</", "<\\/")
    return """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Сравнение моделей: теги экскурсий</title>
<style>
:root { color-scheme: dark; --bg:#111827; --panel:#1f2937; --line:#374151; --muted:#9ca3af; --text:#f9fafb; --accent:#60a5fa; --chip:#26364e; --error:#fecaca; }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 Inter,Segoe UI,Arial,sans-serif }
main { max-width:1600px; margin:auto; padding:28px 20px 60px } h1 { margin:0 0 6px; font-size:26px } .hint { color:var(--muted); margin:0 0 22px }
.toolbar { position:sticky; top:0; z-index:2; background:rgba(17,24,39,.95); backdrop-filter:blur(10px); padding:14px 0; border-bottom:1px solid var(--line); display:flex; gap:18px; flex-wrap:wrap; align-items:center }
.group { display:flex; gap:8px; flex-wrap:wrap; align-items:center } .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em }
label.choice, button { border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:8px; padding:7px 10px; cursor:pointer } label.choice:has(input:checked) { border-color:var(--accent); background:#172a45 } input { accent-color:var(--accent) }
button.active { background:var(--accent); color:#08111e; border-color:var(--accent); font-weight:700 } .summary { color:var(--muted); margin:18px 0 }
.table-wrap { overflow:auto; border:1px solid var(--line); border-radius:12px } table { width:100%; border-collapse:collapse; min-width:1000px; table-layout:fixed } th { background:#162235; text-align:left; color:#bfdbfe } td,th { padding:10px; border-bottom:1px solid var(--line); vertical-align:top; overflow-wrap:anywhere } tr:last-child td { border-bottom:0 } td.tags { min-width:240px }
.chip { display:inline-block; background:var(--chip); border:1px solid #39516e; border-radius:999px; padding:2px 7px; margin:1px; font-size:12px } .chip.common-chip { background:#3b275e; border-color:#a78bfa; color:#ede9fe } .muted { color:var(--muted) } .cost { white-space:nowrap; color:#a7f3d0 } .error { color:var(--error) } .result-title { display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:start; gap:10px } .model-name { min-width:0; overflow-wrap:anywhere } .model-meta { display:flex; align-items:center; gap:7px; flex-wrap:wrap; margin-top:3px } button.copy-body,button.annotation-btn { padding:4px 8px; font-size:11px; margin:0; border-color:#60a5fa; color:#bfdbfe } dialog { width:min(760px,calc(100% - 32px)); color:var(--text); background:var(--panel); border:1px solid #527197; border-radius:14px; padding:20px } dialog::backdrop { background:rgba(0,0,0,.65) } dialog h2 { margin:0 0 12px; font-size:18px } dialog p { white-space:pre-wrap; color:#d1d5db } .dialog-close { float:right }
#cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px } .card { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px } .card h2 { font-size:17px; margin:0 0 7px } .meta { color:var(--muted); margin-bottom:10px } .common { border-left:3px solid var(--accent); padding-left:10px; margin:12px 0 } .result { border-top:1px solid var(--line); padding:11px 0 } .result:first-of-type { border-top:0 } .result-title { display:flex; justify-content:space-between; gap:8px; font-weight:600 } .hidden { display:none !important }
</style>
</head>
<body><main>
<h1>Сравнение моделей: теги экскурсий</h1>
<p class="hint">Фильтры сразу пересчитывают общие теги для выбранных моделей и режимов.</p>
<div class="toolbar"><div class="group"><span class="label">Модели</span><span id="models"></span><button id="clear-models">Снять все</button></div><div class="group"><span class="label">Режимы</span><span id="modes"></span></div><div class="group"><button id="table-btn" class="active">Таблица</button><button id="cards-btn">Карточки</button></div></div>
<div id="summary" class="summary"></div><div id="table-view" class="table-wrap"></div><div id="cards" class="hidden"></div>
<dialog id="annotation-dialog"><button class="dialog-close" id="annotation-close">Закрыть</button><h2 id="annotation-title"></h2><p id="annotation-text"></p></dialog>
</main><script id="report-data" type="application/json">""" + data + """</script>
<script>
const data=JSON.parse(document.getElementById('report-data').textContent), $=s=>document.querySelector(s), LEXICAL='lexical_triggers';
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const chips=(tags,common=[])=>{const shared=new Set(common);return tags.length?tags.map(t=>`<span class="chip ${shared.has(t)?'common-chip':''}">${esc(t)}</span>`).join(''):'<span class="muted">—</span>';};
function controls(target, values, type){ target.innerHTML=values.map(v=>`<label class="choice"><input type="checkbox" data-${type}="${esc(v)}" checked> ${esc(v)}</label>`).join(''); target.querySelectorAll('input').forEach(x=>x.onchange=render); }
function selected(type){ return [...document.querySelectorAll(`[data-${type}]:checked`)].map(x=>x.getAttribute(`data-${type}`)); }
function visibleResults(exp){ const ms=new Set(selected('model')), modes=new Set(selected('mode')); return exp.results.filter(r=>ms.has(r.model)&&(r.model===LEXICAL||modes.has(r.mode))); }
function common(results){ const sets=results.filter(r=>!r.error&&r.model!==LEXICAL).map(r=>new Set(r.tags)); if(sets.length<2) return []; return [...sets.reduce((a,b)=>new Set([...a].filter(x=>b.has(x))))].sort(); }
function resultHtml(r,common){ return r.error?`<span class="error">Ошибка: ${esc(r.error)}</span>`:chips(r.tags,common); }
function copyBody(expId,model,mode,button){ const result=data.experiences.find(e=>e.id===Number(expId))?.results.find(r=>r.model===model&&r.mode===mode); if(!result?.request_body) return; const text=JSON.stringify(result.request_body,null,2); const done=()=>{button.textContent='Скопировано';setTimeout(()=>button.textContent='Копировать prompt body',1200)}; if(navigator.clipboard?.writeText){navigator.clipboard.writeText(text).then(done).catch(()=>fallbackCopy(text,done));}else fallbackCopy(text,done); }
function fallbackCopy(text,done){const area=document.createElement('textarea');area.value=text;document.body.append(area);area.select();document.execCommand('copy');area.remove();done();}
function bindCopyButtons(){ document.querySelectorAll('.copy-body').forEach(button=>button.onclick=()=>copyBody(button.dataset.exp,button.dataset.model,button.dataset.mode,button)); }
function showAnnotation(expId){const exp=data.experiences.find(e=>e.id===Number(expId)); if(!exp) return; $('#annotation-title').textContent=exp.title; $('#annotation-text').textContent=exp.annotation||'Аннотация отсутствует.'; $('#annotation-dialog').showModal();}
function bindAnnotationButtons(){document.querySelectorAll('.annotation-btn').forEach(button=>button.onclick=()=>showAnnotation(button.dataset.exp));}
function render(){ const ms=selected('model'), rows=data.experiences.map(e=>{const rs=visibleResults(e), shared=common(rs), lexical=rs.find(r=>r.model===LEXICAL)?.tags||[]; return {e,rs,common:shared,lexicalCommon:shared.filter(t=>lexical.includes(t))};}); $('#summary').textContent=`Показано моделей: ${ms.length}; режимов: ${selected('mode').length}; экскурсий: ${rows.length}.`;
 $('#table-view').innerHTML=`<table><colgroup><col style="width:16%"><col style="width:24%"><col style="width:18%"><col style="width:30%"><col style="width:6%"><col style="width:6%"></colgroup><thead><tr><th>ID / экскурсия</th><th>Теги Tripster</th><th>Модель / режим</th><th>Теги</th><th>Кол-во</th><th>Стоимость</th></tr></thead><tbody>${rows.flatMap(({e,rs,common,lexicalCommon})=>rs.map((r,index)=>`<tr>${index===0?`<td rowspan="${rs.length}"><b>${e.id}</b><br>${esc(e.title)}<div class="model-meta"><button class="annotation-btn" data-exp="${e.id}">Аннотация</button></div></td><td rowspan="${rs.length}"><span class="muted">Всего категорий: ${e.all_tripster_tags_count}</span><br>${chips(e.old_tripster_tags)}<div class="common"><b>Общие теги выбранных результатов (${common.length})</b><br>${chips(common,common)}<br><b>Общие с lexical (${lexicalCommon.length})</b><br>${chips(lexicalCommon,lexicalCommon)}</div></td>`:''}<td><div class="model-name">${esc(r.model)}</div><div class="model-meta"><span class="muted">${esc(r.mode)} · ${r.tags.length} тег.</span>${r.request_body?`<button class="copy-body" data-exp="${e.id}" data-model="${esc(r.model)}" data-mode="${esc(r.mode)}">Копировать prompt body</button>`:''}</div></td><td class="tags">${resultHtml(r,common)}</td><td>${r.tags.length}</td><td class="cost">${esc(r.cost)}</td></tr>`)).join('')||'<tr><td colspan="6" class="muted">Нет выбранных результатов.</td></tr>'}</tbody></table>`;
 $('#cards').innerHTML=rows.map(({e,rs,common,lexicalCommon})=>`<article class="card"><h2>${esc(e.title)}</h2><div class="meta">ID: ${e.id} <button class="annotation-btn" data-exp="${e.id}">Аннотация</button><br>Tripster: ${chips(e.old_tripster_tags)}<br>Всего категорий Tripster: ${e.all_tripster_tags_count}</div><div class="common"><b>Общие теги выбранных результатов (${common.length})</b><br>${chips(common,common)}<br><b>Общие с lexical (${lexicalCommon.length})</b><br>${chips(lexicalCommon,lexicalCommon)}</div>${rs.map(r=>`<div class="result"><div class="result-title"><div><div class="model-name">${esc(r.model)}</div><div class="model-meta"><span class="muted">${esc(r.mode)} · ${r.tags.length} тег.</span>${r.request_body?`<button class="copy-body" data-exp="${e.id}" data-model="${esc(r.model)}" data-mode="${esc(r.mode)}">Копировать prompt body</button>`:''}</div></div><span class="cost">${esc(r.cost)}</span></div><div>${resultHtml(r,common)}</div></div>`).join('')||'<span class="muted">Нет выбранных результатов.</span>'}</article>`).join(''); bindCopyButtons(); bindAnnotationButtons(); }
controls($('#models'),data.models,'model'); controls($('#modes'),data.modes,'mode'); $('#clear-models').onclick=()=>{document.querySelectorAll('[data-model]').forEach(x=>x.checked=false);render();}; $('#annotation-close').onclick=()=>$('#annotation-dialog').close(); $('#table-btn').onclick=()=>{ $('#table-view').classList.remove('hidden'); $('#cards').classList.add('hidden'); $('#table-btn').classList.add('active'); $('#cards-btn').classList.remove('active'); }; $('#cards-btn').onclick=()=>{ $('#cards').classList.remove('hidden'); $('#table-view').classList.add('hidden'); $('#cards-btn').classList.add('active'); $('#table-btn').classList.remove('active'); }; render();
</script></body></html>"""


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    args = parse_args()
    if args.limit != DEFAULT_SAMPLE_SIZE and not args.experience_id:
        raise RuntimeError("For the default comparison use one random experience; omit --limit or set --limit 1.")
    if args.workers < 1:
        raise RuntimeError("--workers must be >= 1")
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("Set LLM_API_KEY in .env")

    default_models = [*DEFAULT_MODELS, *EXTRA_MODELS] if args.include_extra_models else list(DEFAULT_MODELS)
    models = list(dict.fromkeys(args.model or default_models))
    if len(models) < 2:
        raise RuntimeError("Specify at least two models, for example: --model model-a --model model-b")
    modes = list(dict.fromkeys(args.mode or ["no-cache", "no-commons", "embedding-no-commons", "embedding-commons"]))
    db = supabase_from_env(args.timeout, args.max_retries)
    experiences = fetch_random_experiences(db, args.limit, parse_ids(args.experience_id), random.Random(args.seed))
    attach_tripster_common_tags(db, experiences)
    allowed_sub_slugs, _ = fetch_allowed_catalog(db)
    lexical_catalog = fetch_lexical_catalog(db)
    embedding_catalogs: dict[int, list[str]] = {}
    if any(mode.startswith("embedding-") for mode in modes):
        embedding_catalogs = {
            int(experience["id"]): embedding_catalog_for_experience(db, experience, args.timeout, args.max_retries)
            for experience in experiences
        }
        if any(len(items) < EMBEDDING_TOP_K for items in embedding_catalogs.values()):
            log("Warning: embedding search returned fewer than 120 unique categories for at least one experience")
    log(f"Selected experience_ids={','.join(str(item['id']) for item in experiences)}")
    log(f"Variants: {', '.join(f'{model}/{mode}' for model in models for mode in modes)}")
    log(f"Planned LLM requests: {len(experiences) * len(models) * len(modes)}")

    variants: dict[tuple[str, str], dict[int, dict[str, Any]]] = {
        (model, mode): {int(experience["id"]): {} for experience in experiences} for model in models for mode in modes
    }
    request_count = len(experiences) * len(models) * len(modes)
    with ThreadPoolExecutor(max_workers=min(args.workers, request_count)) as executor:
        futures = [
            executor.submit(
                run_request,
                model,
                mode,
                experience,
                embedding_catalogs[int(experience["id"])] if mode.startswith("embedding-") else allowed_sub_slugs,
                api_key,
                args,
            )
            for model in models
            for mode in modes
            for experience in experiences
        ]
        for future in as_completed(futures):
            model, mode, experience_id, row = future.result()
            variants[(model, mode)][experience_id] = row
            log(f"Completed: {model} / {mode} / experience_id={experience_id}")

    add_lexical_results(variants, experiences, lexical_catalog)
    log(f"Completed: {LEXICAL_MODEL} / categories_with_triggers={len(lexical_catalog)}")

    print()
    print(render_table(experiences, variants))
    report_path = Path(args.html_out).resolve()
    report_path.write_text(render_html_report(experiences, variants), encoding="utf-8")
    log(f"HTML report: {report_path}")
    if not args.no_open:
        webbrowser.open(report_path.as_uri())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
