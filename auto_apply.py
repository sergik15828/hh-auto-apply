#!/usr/bin/env python3
"""
HH.ru auto search and response helper.

Search is performed through the public hh.ru API. Real vacancy responses are
sent through a saved Playwright browser session from hh_login.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT_DIR = Path(__file__).resolve().parent
MY_DIR = ROOT_DIR / "my"
DEFAULT_CONFIG_PATH = MY_DIR / "config.yaml"
DEFAULT_STATE_DB = ROOT_DIR / "data" / "hh_auto_apply.sqlite3"
DEFAULT_COVER_LETTER_PROMPT_PATH = MY_DIR / "cover_letter_prompt.md"
HH_API_BASE = "https://api.hh.ru"
DEFAULT_USER_AGENT = "hh-auto-apply/1.0 (contact: set HH_USER_AGENT in .env)"
HH_API_TIMEOUT_SECONDS = 45
HH_API_RETRIES = 3

HR_ADAPTATION_RULES = """
Ты — профессиональный HR-консультант и эксперт по резюме. Твоя задача — адаптировать
подачу кандидата под конкретную вакансию в сопроводительном письме.

Общие принципы:
- Краткость: излагай информацию сжато, без воды.
- Конкретность: предпочитай измеримые достижения общим фразам.
- Релевантность: содержание должно соответствовать требованиям вакансии.
- Правдивость: не преувеличивай опыт и навыки.
- Деловой стиль: исключи юмор, сленг и восклицательные знаки.
- Не используй букву "е" с точками, длинные тире и типографские тире.

Алгоритм адаптации:
- Сопоставь желаемую роль кандидата с названием вакансии.
- Естественно встрой ключевые слова из вакансии, если они подтверждаются профилем.
- На первое место ставь релевантные обязанности, проекты и достижения.
- Не упоминай нерелевантный опыт, если он не помогает отклику.
- Используй цифры и результаты только если они есть в оригинальном профиле.

Не включай:
- Названия прошлых компаний кандидата.
- Семейное положение и возраст.
- Хобби, если они не связаны с работой.
- Очевидные навыки вроде MS Office или "уверенный пользователь ПК".
- Нерелевантный опыт.

Критически важный запрет галлюцинаций:
- Строго запрещено выдумывать факты, компании, должности, проекты или навыки.
- Строго запрещено добавлять образование, сертификаты или курсы, которых нет в профиле.
- Строго запрещено придумывать метрики и цифры.
- Используй только информацию из профиля кандидата и вакансии.
- Можно перефразировать и реструктурировать, но нельзя добавлять несуществующие данные.
- Нельзя использовать названия прошлых компаний кандидата в письме.
""".strip()


class HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return normalize_space(" ".join(self.parts))


@dataclass(frozen=True)
class Vacancy:
    id: str
    title: str
    employer: str
    url: str
    apply_url: str
    description: str
    has_test: bool
    response_letter_required: bool
    query: str
    schedule_id: str
    schedule_name: str


@dataclass(frozen=True)
class LlmConfig:
    provider: str
    model: str
    api_key: str


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_letter_text(value: str) -> str:
    replacements = {
        "ё": "е",
        "Ё": "Е",
        "—": "-",
        "–": "-",
        "−": "-",
        "‑": "-",
    }
    for source, replacement in replacements.items():
        value = value.replace(source, replacement)
    return normalize_space(value)


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config must contain a YAML object: {path}")
    return data


def get_nested(config: dict[str, Any], path: str, default: Any) -> Any:
    current: Any = config
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def hh_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {key: value for key, value in (params or {}).items() if value is not None},
        doseq=True,
    )
    url = f"{HH_API_BASE}{path}"
    if query:
        url = f"{url}?{query}"

    last_error: Exception | None = None
    for attempt in range(1, HH_API_RETRIES + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": hh_user_agent(),
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=HH_API_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HH API error {exc.code}: {body[:500]}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt < HH_API_RETRIES:
                print(f"HH API timeout/network error, retry {attempt}/{HH_API_RETRIES}: {url}")
                time.sleep(2 * attempt)

    raise RuntimeError(f"HH API network error after {HH_API_RETRIES} attempts: {last_error}") from last_error


def hh_user_agent() -> str:
    return os.getenv("HH_USER_AGENT") or DEFAULT_USER_AGENT


def strip_html(value: str) -> str:
    parser = HtmlTextExtractor()
    parser.feed(html.unescape(value or ""))
    return parser.text()


def build_title_query(query: str, title_only: bool) -> str:
    query = query.strip()
    return query


def vacancy_rules(config: dict[str, Any]) -> dict[str, Any]:
    return get_nested(config, "vacancies", {})


def vacancy_keywords(config: dict[str, Any]) -> list[str]:
    rules = vacancy_rules(config)
    if "keywords" in rules:
        return [str(value).strip() for value in rules.get("keywords") or [] if str(value).strip()]
    search_config = get_nested(config, "search", {})
    return [str(value).strip() for value in search_config.get("queries") or [] if str(value).strip()]


def vacancy_stop_words(config: dict[str, Any]) -> list[str]:
    rules = vacancy_rules(config)
    if "stop_words" in rules:
        return [str(value).strip() for value in rules.get("stop_words") or [] if str(value).strip()]
    filters = get_nested(config, "filters", {})
    return [str(value).strip() for value in filters.get("exclude_title_keywords") or [] if str(value).strip()]


def required_title_words_any(config: dict[str, Any]) -> list[str]:
    rules = vacancy_rules(config)
    return [str(value).strip() for value in rules.get("required_title_words_any") or [] if str(value).strip()]


def remote_only_enabled(config: dict[str, Any]) -> bool:
    rules = vacancy_rules(config)
    return bool(rules.get("remote_only", False))


def skip_already_applied_enabled(config: dict[str, Any]) -> bool:
    rules = vacancy_rules(config)
    return bool(rules.get("skip_already_applied", True))


def page_has_existing_response(page: Page) -> bool:
    response_markers = [
        "text=Вы откликнулись",
        "text=Отклик отправлен",
        "text=Резюме доставлено",
        "text=Вы уже откликнулись",
    ]
    for marker in response_markers:
        try:
            if page.locator(marker).count() > 0:
                return True
        except Exception:
            continue
    return False


def keyword_match(value: str, keywords: list[str]) -> str | None:
    low_value = value.lower()
    for keyword in keywords:
        if keyword and keyword.lower() in low_value:
            return keyword
    return None


def fetch_vacancy_details(vacancy_id: str) -> dict[str, Any]:
    return hh_get(f"/vacancies/{vacancy_id}")


def vacancy_passes_filters(
    config: dict[str, Any],
    title: str,
    employer: str,
    description: str,
    schedule_id: str,
    has_test: bool,
) -> bool:
    filters = get_nested(config, "filters", {})
    if remote_only_enabled(config) and schedule_id != "remote":
        return False
    if required_title_words_any(config) and keyword_match(title, required_title_words_any(config)) is None:
        return False
    if keyword_match(title, vacancy_stop_words(config)):
        return False
    if keyword_match(employer, filters.get("exclude_company_keywords") or []):
        return False
    if keyword_match(description, filters.get("exclude_description_keywords") or []):
        return False
    if bool(filters.get("skip_has_test", True)) and has_test:
        return False
    return True


def search_vacancies(config: dict[str, Any], conn: sqlite3.Connection | None = None) -> list[Vacancy]:
    try:
        return search_vacancies_api(config)
    except RuntimeError as exc:
        print(f"HH API search failed: {exc}")
        print("Falling back to hh.ru browser search...")
        state_path = session_file()
        if not state_path.exists():
            raise RuntimeError(f"HH session not found: {state_path}. Run python3 hh_login.py") from exc
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, slow_mo=250)
            context = browser.new_context(storage_state=str(state_path))
            page = context.new_page()
            try:
                return search_vacancies_browser(page, config, conn)
            finally:
                context.close()
                browser.close()


def search_vacancies_api(config: dict[str, Any]) -> list[Vacancy]:
    search_config = get_nested(config, "search", {})
    queries = vacancy_keywords(config)
    if not queries:
        raise ValueError("Config vacancies.keywords is empty")

    results: list[Vacancy] = []
    seen_ids: set[str] = set()
    title_only = bool(search_config.get("title_only", True))
    max_pages = int(search_config.get("max_pages", 1))
    per_page = int(search_config.get("per_page", 20))
    remote_only = remote_only_enabled(config)

    for raw_query in queries:
        query = str(raw_query).strip()
        if not query:
            continue

        for page_num in range(max_pages):
            response = hh_get(
                "/vacancies",
                {
                    "text": build_title_query(query, title_only),
                    "area": search_config.get("area", 113),
                    "per_page": per_page,
                    "page": page_num,
                    "search_field": "name" if title_only else search_config.get("search_field"),
                    "order_by": search_config.get("order_by", "publication_time"),
                    "period": search_config.get("period_days", 7),
                    "schedule": "remote" if remote_only else search_config.get("schedule"),
                },
            )

            for item in response.get("items", []):
                vacancy_id = str(item.get("id") or "")
                if not vacancy_id or vacancy_id in seen_ids:
                    continue
                seen_ids.add(vacancy_id)

                details = fetch_vacancy_details(vacancy_id)
                title = normalize_space(str(details.get("name") or item.get("name") or ""))
                employer = normalize_space(
                    str((details.get("employer") or item.get("employer") or {}).get("name") or "")
                )
                description = strip_html(str(details.get("description") or ""))
                schedule = details.get("schedule") or item.get("schedule") or {}
                schedule_id = str(schedule.get("id") or "")
                schedule_name = str(schedule.get("name") or "")

                if not vacancy_passes_filters(
                    config,
                    title=title,
                    employer=employer,
                    description=description,
                    schedule_id=schedule_id,
                    has_test=bool(details.get("has_test") or item.get("has_test")),
                ):
                    continue

                results.append(
                    Vacancy(
                        id=vacancy_id,
                        title=title,
                        employer=employer or "Компания",
                        url=str(details.get("alternate_url") or item.get("alternate_url") or ""),
                        apply_url=str(
                            details.get("apply_alternate_url")
                            or item.get("apply_alternate_url")
                            or details.get("alternate_url")
                            or item.get("alternate_url")
                            or ""
                        ),
                        description=description,
                        has_test=bool(details.get("has_test") or item.get("has_test")),
                        response_letter_required=bool(
                            details.get("response_letter_required")
                            or item.get("response_letter_required")
                        ),
                        query=query,
                        schedule_id=schedule_id,
                        schedule_name=schedule_name,
                    )
                )

    return results


def search_vacancies_browser(
    page: Page,
    config: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> list[Vacancy]:
    search_config = get_nested(config, "search", {})
    queries = vacancy_keywords(config)
    if not queries:
        raise ValueError("Config vacancies.keywords is empty")

    results: list[Vacancy] = []
    seen_ids: set[str] = set()
    title_only = bool(search_config.get("title_only", True))
    max_pages = int(search_config.get("max_pages", 1))
    per_page = int(search_config.get("per_page", 20))

    for query in queries:
        for page_num in range(max_pages):
            params = {
                "text": query,
                "area": search_config.get("area", 113),
                "items_on_page": per_page,
                "page": page_num,
            }
            if title_only:
                params["search_field"] = "name"
            if remote_only_enabled(config):
                params["schedule"] = "remote"

            url = "https://hh.ru/search/vacancy?" + urllib.parse.urlencode(params)
            try:
                page.goto(url, wait_until="commit", timeout=90_000)
                page.wait_for_load_state("domcontentloaded", timeout=20_000)
            except PlaywrightTimeoutError:
                print(f"Browser search page load timeout, trying to parse current page: {url}")
            page.wait_for_timeout(2000)

            if "captcha" in page.title().lower() or page.locator("text=Капча").count() > 0:
                raise RuntimeError("hh.ru captcha appeared during browser search")

            cards = page.locator("[data-qa='vacancy-serp__vacancy']").all()
            if not cards:
                cards = page.locator("[data-qa='serp-item']").all()

            candidates: list[tuple[str, str, str, str]] = []
            for card in cards:
                try:
                    title_el = card.locator("[data-qa='serp-item__title']").first
                    if title_el.count() == 0:
                        continue
                    title = normalize_space(title_el.inner_text())
                    vacancy_url = str(title_el.get_attribute("href") or "")
                    vacancy_id = extract_vacancy_id(vacancy_url)
                    if not vacancy_id or vacancy_id in seen_ids:
                        continue
                    seen_ids.add(vacancy_id)

                    employer_el = card.locator("[data-qa='vacancy-serp__vacancy-employer']").first
                    employer = normalize_space(employer_el.inner_text()) if employer_el.count() > 0 else "Компания"
                    candidates.append((vacancy_id, title, employer, vacancy_url))
                except Exception:
                    continue

            for vacancy_id, title, employer, vacancy_url in candidates:
                description, already_responded = get_vacancy_description_browser(page, vacancy_url)
                if already_responded:
                    print(f"Skipping already responded vacancy: {title}")
                    if conn is not None:
                        record_result(
                            conn,
                            Vacancy(
                                id=vacancy_id,
                                title=title,
                                employer=employer or "Компания",
                                url=vacancy_url,
                                apply_url=vacancy_url,
                                description=description,
                                has_test=False,
                                response_letter_required=False,
                                query=query,
                                schedule_id="remote" if remote_only_enabled(config) else "",
                                schedule_name="Удаленная работа" if remote_only_enabled(config) else "",
                            ),
                            "skipped",
                            "Already responded on hh.ru",
                            "",
                        )
                    continue
                schedule_id = "remote" if remote_only_enabled(config) else ""
                schedule_name = "Удаленная работа" if remote_only_enabled(config) else ""
                if not vacancy_passes_filters(
                    config,
                    title=title,
                    employer=employer,
                    description=description,
                    schedule_id=schedule_id,
                    has_test=False,
                ):
                    continue
                results.append(
                    Vacancy(
                        id=vacancy_id,
                        title=title,
                        employer=employer or "Компания",
                        url=vacancy_url,
                        apply_url=vacancy_url,
                        description=description,
                        has_test=False,
                        response_letter_required=False,
                        query=query,
                        schedule_id=schedule_id,
                        schedule_name=schedule_name,
                    )
                )

    return results


def extract_vacancy_id(url: str) -> str:
    match = re.search(r"/vacancy/(\d+)", url)
    if match:
        return match.group(1)
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("vacancyId", "vacancy_id", "id"):
        if query.get(key):
            return str(query[key][0])
    return ""


def get_vacancy_description_browser(page: Page, url: str) -> tuple[str, bool]:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1000)
        already_responded = page_has_existing_response(page)
        page.wait_for_selector("[data-qa='vacancy-description']", timeout=10_000)
        desc_el = page.locator("[data-qa='vacancy-description']").first
        description = normalize_space(desc_el.inner_text()) if desc_el.count() > 0 else ""
        return description, already_responded
    except Exception:
        return "", False


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vacancy_runs (
            vacancy_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            reason TEXT,
            title TEXT NOT NULL,
            employer TEXT NOT NULL,
            url TEXT NOT NULL,
            query TEXT NOT NULL,
            letter TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def already_processed(conn: sqlite3.Connection, vacancy_id: str, include_dry_run: bool) -> bool:
    statuses = ["success", "skipped"]
    if include_dry_run:
        statuses.append("dry_run")
    placeholders = ",".join("?" for _ in statuses)
    row = conn.execute(
        f"SELECT 1 FROM vacancy_runs WHERE vacancy_id = ? AND status IN ({placeholders})",
        (vacancy_id, *statuses),
    ).fetchone()
    return row is not None


def record_result(
    conn: sqlite3.Connection,
    vacancy: Vacancy,
    status: str,
    reason: str,
    letter: str,
) -> None:
    conn.execute(
        """
        INSERT INTO vacancy_runs (
            vacancy_id, status, reason, title, employer, url, query, letter, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vacancy_id) DO UPDATE SET
            status = excluded.status,
            reason = excluded.reason,
            title = excluded.title,
            employer = excluded.employer,
            url = excluded.url,
            query = excluded.query,
            letter = excluded.letter,
            updated_at = excluded.updated_at
        """,
        (
            vacancy.id,
            status,
            reason,
            vacancy.title,
            vacancy.employer,
            vacancy.url,
            vacancy.query,
            letter,
            dt.datetime.now(dt.timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def read_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Install pypdf or create my/profile.md with your profile text") from exc

    reader = PdfReader(str(path))
    chunks: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            chunks.append(text)
    return normalize_space("\n".join(chunks))


def load_profile() -> str:
    profile_md = MY_DIR / "profile.md"
    if profile_md.exists():
        return profile_md.read_text(encoding="utf-8").strip()

    pdf_paths = sorted(MY_DIR.glob("*.pdf"))
    chunks: list[str] = []
    for path in pdf_paths:
        text = read_pdf_text(path)
        if text:
            chunks.append(f"Источник: {path.name}\n{text}")

    profile = "\n\n".join(chunks).strip()
    if not profile:
        raise RuntimeError("No profile found. Add my/profile.md or a readable PDF resume to my/")
    return profile


def load_cover_letter_prompt_template(config: dict[str, Any]) -> str:
    letter_config = get_nested(config, "letter", {})
    prompt_path = Path(letter_config.get("prompt_path") or DEFAULT_COVER_LETTER_PROMPT_PATH)
    if not prompt_path.is_absolute():
        prompt_path = ROOT_DIR / prompt_path
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8").strip()
    return default_cover_letter_prompt_template()


def default_cover_letter_prompt_template() -> str:
    return """
Ты пишешь сопроводительные письма для hh.ru от лица кандидата.

Главная цель: письмо должно выглядеть как короткое осмысленное сообщение живого senior/lead-разработчика, а не как универсальный шаблон.

Стиль:
- русский язык;
- спокойно, уверенно, профессионально;
- без восторга, продажности, канцелярита и HR-штампов;
- без фраз "меня заинтересовала вакансия", "буду рад", "рассмотрите мою кандидатуру", "внести вклад", "с большим интересом";
- без восклицательных знаков;
- не пересказывай резюме целиком.

Логика письма:
1. Начни с короткого приветствия.
2. Сразу назови 1-2 точки совпадения между вакансией и опытом кандидата.
3. Добавь конкретный релевантный опыт или результат из профиля.
4. Заверши спокойным предложением обсудить задачи команды.
""".strip()


def build_cover_letter_prompt(
    profile: str,
    vacancy: Vacancy,
    config: dict[str, Any],
) -> tuple[str, str, int]:
    letter_config = get_nested(config, "letter", {})
    max_chars = int(letter_config.get("max_chars", 1200))
    portfolio_url = str(letter_config.get("portfolio_url") or "").strip()
    extra_instructions = str(letter_config.get("extra_instructions") or "").strip()

    system = f"{load_cover_letter_prompt_template(config)}\n\n{HR_ADAPTATION_RULES}".strip()
    user = f"""
Профиль кандидата:
{profile[:6000]}

Вакансия:
Название: {vacancy.title}
Компания: {vacancy.employer}
Описание: {vacancy.description[:4000]}

Требования к письму:
- 3-5 предложений.
- Максимум {max_chars} символов.
- Не начинай с "Меня заинтересовала вакансия".
- Не пиши общими словами "имею большой опыт", если можно назвать стек, домен или задачу.
- Упомяни только 1-2 наиболее релевантных факта из профиля.
- Если вакансия про PHP/Laravel/WordPress, делай акцент на разработке, API, внутренних сервисах, WordPress и performance.
- Если вакансия про Team Lead, делай акцент на руководстве командой 10+, code review, процессах и менторинге.
- Если вакансия про DevOps/инфраструктуру, делай акцент на Linux, Nginx/Apache, Docker, CI/CD, Cloudflare, DDoS и нагрузках.
- Если нечего сопоставить, напиши нейтрально и не притягивай опыт.

Верни только текст письма, без заголовков и пояснений.
"""
    if portfolio_url:
        user += f"\n- Можно аккуратно добавить ссылку на портфолио: {portfolio_url}\n"
    if extra_instructions:
        user += f"\nДополнительные инструкции:\n{extra_instructions}\n"

    return system, user, max_chars


def load_llm_config() -> LlmConfig:
    provider = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()
    if provider == "none":
        return LlmConfig(provider=provider, model="none", api_key="")
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY") or ""
        model = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    elif provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY") or ""
        model = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-4-6"
    else:
        raise RuntimeError("LLM_PROVIDER must be either 'openai' or 'anthropic'")

    if provider != "none" and not api_key:
        env_name = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        raise RuntimeError(f"{env_name} is missing for LLM_PROVIDER={provider}")

    return LlmConfig(provider=provider, model=model, api_key=api_key)


def generate_cover_letter(
    llm: LlmConfig,
    profile: str,
    vacancy: Vacancy,
    config: dict[str, Any],
) -> str:
    if llm.provider == "none":
        return normalize_letter_text(generate_fallback_letter(vacancy))

    system, user, max_chars = build_cover_letter_prompt(profile, vacancy, config)
    if llm.provider == "openai":
        letter = generate_openai_letter(llm, system, user)
    elif llm.provider == "anthropic":
        letter = generate_anthropic_letter(llm, system, user)
    else:
        raise RuntimeError(f"Unsupported LLM provider: {llm.provider}")
    return normalize_letter_text(letter)[:max_chars].strip()


def generate_fallback_letter(vacancy: Vacancy) -> str:
    title = vacancy.title.lower()
    focus: list[str] = []
    if any(word in title for word in ("lead", "лид", "руковод", "team")):
        focus.append("руководство командой 10+ разработчиков, code review, процессы и менторинг")
    if any(word in title for word in ("laravel", "php", "backend", "бэкенд")):
        focus.append("PHP/Laravel, внутренние сервисы, REST API и интеграции")
    if "wordpress" in title or "wp" in title:
        focus.append("кастомная WordPress-разработка, техническое SEO и оптимизация загрузки")
    if any(word in title for word in ("devops", "cloudflare", "linux", "infra", "инфра")):
        focus.append("Linux/Nginx/Apache, Docker, CI/CD, Cloudflare, DDoS-защита и нагрузки")

    if not focus:
        focus.append("backend-разработка, инфраструктура и техническое лидерство")
    focus_text = "; ".join(focus[:2])

    return (
        f"Здравствуйте. По вакансии {vacancy.title} вижу хорошее совпадение с моим опытом: {focus_text}. "
        "У меня 10+ лет в веб-разработке, последние 4 года я работал как Fullstack PHP Developer / Team Lead "
        "в affiliate и iGaming, руководил командой 10+ разработчиков и отвечал за разработку, инфраструктуру "
        "и устойчивость проектов под нагрузкой. Могу быть полезен там, где нужно не только писать код, "
        "но и выстраивать технические решения, процессы и качество разработки. Готов обсудить задачи команды."
    )


def generate_openai_letter(llm: LlmConfig, system: str, user: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=llm.api_key)
    response = client.chat.completions.create(
        model=llm.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.5,
        max_tokens=450,
    )
    return (response.choices[0].message.content or "").strip()


def generate_anthropic_letter(llm: LlmConfig, system: str, user: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=llm.api_key)
    response = client.messages.create(
        model=llm.model,
        system=system,
        messages=[{"role": "user", "content": user}],
        max_tokens=450,
        temperature=0.5,
    )
    chunks: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            chunks.append(getattr(block, "text", ""))
    return "".join(chunks).strip()


def locator_context_text(locator) -> str:
    try:
        return normalize_space(
            locator.evaluate(
                """element => {
                    let node = element;
                    const parts = [];
                    for (let i = 0; i < 5 && node; i += 1) {
                        if (node.innerText) parts.push(node.innerText);
                        node = node.parentElement;
                    }
                    return parts.join(' ');
                }"""
            )
        )
    except Exception:
        return ""


def locator_value(locator) -> str:
    try:
        value = locator.input_value(timeout=500)
        return value or ""
    except Exception:
        return ""


def visible_textareas(page: Page):
    result = []
    textareas = page.locator("textarea")
    for index in range(textareas.count()):
        textarea = textareas.nth(index)
        try:
            if textarea.is_visible(timeout=500):
                result.append(textarea)
        except Exception:
            continue
    return result


def find_cover_letter_textarea(page: Page):
    textareas = visible_textareas(page)
    for textarea in textareas:
        context = locator_context_text(textarea).lower()
        try:
            placeholder = (textarea.get_attribute("placeholder") or "").lower()
        except Exception:
            placeholder = ""
        combined = f"{context} {placeholder}"
        if "сопровод" in combined or "письм" in combined:
            return textarea
    if len(textareas) == 1:
        context = locator_context_text(textareas[0]).lower()
        if not any(word in context for word in ("город", "зарплат", "зп", "ожидания", "доход")):
            return textareas[0]
    return None


def configured_question_answers(config: dict[str, Any]) -> list[dict[str, Any]]:
    question_config = get_nested(config, "application_questions", {})
    answers = list(question_config.get("answers") or [])
    city = str(question_config.get("city") or "").strip()
    salary = str(question_config.get("salary_expectations") or "").strip()
    if city:
        answers.append({"keywords": ["город", "откуда", "проживаете"], "answer": city})
    if salary:
        answers.append(
            {
                "keywords": ["зарплат", "зп", "ожидания", "доход", "компенсац"],
                "answer": salary,
            }
        )
    return answers


def answer_for_question(question_text: str, config: dict[str, Any]) -> str:
    normalized = question_text.lower()
    for item in configured_question_answers(config):
        keywords = [str(keyword).lower() for keyword in item.get("keywords") or []]
        if keywords and any(keyword in normalized for keyword in keywords):
            return str(item.get("answer") or "").strip()
    return ""


def fill_application_questions(page: Page, config: dict[str, Any]) -> list[str]:
    filled: list[str] = []
    fields = []
    for selector in ["textarea", "input[type='text']", "input:not([type])"]:
        locators = page.locator(selector)
        for index in range(locators.count()):
            field = locators.nth(index)
            try:
                if field.is_visible(timeout=500) and field.is_enabled(timeout=500):
                    fields.append(field)
            except Exception:
                continue

    for field in fields:
        if locator_value(field).strip():
            continue
        context = locator_context_text(field)
        answer = answer_for_question(context, config)
        if not answer:
            continue
        try:
            field.fill(answer)
            filled.append(f"{context[:80]} -> {answer}")
        except Exception:
            continue
    return filled


def click_first(page: Page, selectors: list[str], timeout_ms: int = 1500) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0 and locator.is_visible(timeout=timeout_ms):
                locator.click()
                return True
        except Exception:
            continue
    return False


def click_first_enabled_button(page: Page, button_texts: list[str], timeout_ms: int = 1500) -> bool:
    for text in button_texts:
        buttons = page.locator(f"button:has-text('{text}')")
        try:
            count = buttons.count()
        except Exception:
            continue
        for index in range(count - 1, -1, -1):
            button = buttons.nth(index)
            try:
                if button.is_visible(timeout=timeout_ms) and button.is_enabled(timeout=timeout_ms):
                    button.click()
                    return True
            except Exception:
                continue
    return False


def save_apply_debug(page: Page, vacancy_id: str) -> str:
    debug_dir = ROOT_DIR / "data" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = debug_dir / f"apply-{vacancy_id}-{stamp}"
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    try:
        base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    return str(base)


def apply_to_vacancy(page: Page, vacancy: Vacancy, letter: str, config: dict[str, Any]) -> tuple[str, str]:
    target_url = vacancy.url or vacancy.apply_url
    if not target_url:
        return "error", "No apply URL"

    page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(1500)

    if page_has_existing_response(page):
        return "skipped", "Already responded"

    clicked_initial = click_first(
        page,
        [
            "[data-qa='vacancy-response-link-top']",
            "[data-qa='vacancy-response-link-bottom']",
            "a:has-text('Откликнуться')",
            "button:has-text('Откликнуться')",
        ],
        timeout_ms=3000,
    )
    if not clicked_initial:
        return "error", "Initial response button not found"

    page.wait_for_timeout(2500)
    if page_has_existing_response(page):
        return "success", "Response sent"

    cover_letter_area = find_cover_letter_textarea(page)
    if cover_letter_area is None:
        click_first(
            page,
            [
                "a:has-text('Написать сопроводительное')",
                "button:has-text('Написать сопроводительное')",
                "a:has-text('Добавить сопроводительное')",
                "button:has-text('Добавить сопроводительное')",
                "button:has-text('С сопроводительным')",
                "text=С сопроводительным",
                "[data-qa='vacancy-response-actions-dropdown']",
            ],
        )
        page.wait_for_timeout(1500)
        cover_letter_area = find_cover_letter_textarea(page)

    if cover_letter_area is not None:
        cover_letter_area.fill(letter)
        page.wait_for_timeout(500)

    filled_questions = fill_application_questions(page, config)
    for filled in filled_questions:
        print(f"Filled application question: {filled}")

    clicked = click_first(
        page,
        [
            "button[data-qa='vacancy-response-submit-popup']",
            "button[data-qa='vacancy-response-submit']",
            "button:has-text('Отправить')",
            "button:has-text('Отправить отклик')",
            "button[type='submit']",
        ],
        timeout_ms=2500,
    )
    if not clicked:
        clicked = click_first_enabled_button(page, ["Откликнуться", "Отправить"], timeout_ms=2500)
    if not clicked:
        debug_base = save_apply_debug(page, vacancy.id)
        return "error", f"Final submit button not found; debug saved: {debug_base}"

    for _ in range(10):
        page.wait_for_timeout(1000)
        if page_has_existing_response(page):
            return "success", "Response sent"

    debug_base = save_apply_debug(page, vacancy.id)
    return "error", f"Submit clicked but hh.ru did not confirm response; debug saved: {debug_base}"


def session_file() -> Path:
    n8n_files_dir = os.getenv("N8N_FILES_DIR") or str(Path.home() / ".n8n-files")
    return Path(n8n_files_dir) / "hh_session.json"


def run_once(config: dict[str, Any], args: argparse.Namespace) -> None:
    state_db = Path(os.getenv("HH_STATE_DB") or args.state_db or DEFAULT_STATE_DB)
    if not state_db.is_absolute():
        state_db = ROOT_DIR / state_db
    conn = init_db(state_db)
    profile = load_profile()
    vacancies = search_vacancies(config, conn)
    limits = get_nested(config, "limits", {})
    max_per_run = int(args.max_applications or limits.get("max_applications_per_run", 5))
    delay = int(limits.get("delay_between_applications_seconds", 12))

    print(f"Found vacancies: {len(vacancies)}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"State DB: {state_db}")

    llm = load_llm_config()
    print(f"LLM provider: {llm.provider} ({llm.model})")

    sent_or_planned = 0
    browser_context = None
    playwright = None
    browser = None
    page = None

    try:
        if args.apply:
            state_path = session_file()
            if not state_path.exists():
                raise RuntimeError(f"HH session not found: {state_path}. Run python3 hh_login.py")
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=bool(args.headless), slow_mo=250)
            browser_context = browser.new_context(storage_state=str(state_path))
            page = browser_context.new_page()

        for vacancy in vacancies:
            if sent_or_planned >= max_per_run:
                break
            if skip_already_applied_enabled(config) and already_processed(
                conn,
                vacancy.id,
                include_dry_run=not args.apply,
            ):
                continue

            print(f"\n{vacancy.title} | {vacancy.employer}")
            if vacancy.schedule_name:
                print(f"Format: {vacancy.schedule_name}")
            print(vacancy.url)
            try:
                letter = generate_cover_letter(llm, profile, vacancy, config)
            except Exception as exc:
                record_result(conn, vacancy, "error", f"Letter generation failed: {exc}", "")
                print(f"Letter generation failed: {exc}")
                if args.apply:
                    print("Skipping apply because letter generation failed.")
                    continue
                letter = ""
            print(f"Letter:\n{letter}\n")

            if args.apply:
                assert page is not None
                status, reason = apply_to_vacancy(page, vacancy, letter, config)
                record_result(conn, vacancy, status, reason, letter)
                print(f"Result: {status} ({reason})")
                time.sleep(delay)
            else:
                record_result(conn, vacancy, "dry_run", "Generated letter only", letter)
                print("Result: dry_run")

            sent_or_planned += 1
    finally:
        if browser_context is not None:
            browser_context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        conn.close()


def seconds_until_next_run(run_times: list[str]) -> int:
    now = dt.datetime.now()
    candidates: list[dt.datetime] = []
    for value in run_times:
        hour, minute = [int(part) for part in value.split(":", 1)]
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
        candidates.append(candidate)
    next_run = min(candidates)
    return max(1, int((next_run - now).total_seconds()))


def run_schedule(config: dict[str, Any], args: argparse.Namespace) -> None:
    run_times = get_nested(config, "schedule.run_times", ["09:30", "18:30"])
    if not isinstance(run_times, list) or not run_times:
        raise ValueError("schedule.run_times must be a non-empty list")

    print(f"Scheduler started. Run times: {', '.join(run_times)}")
    while True:
        sleep_for = seconds_until_next_run([str(value) for value in run_times])
        next_at = dt.datetime.now() + dt.timedelta(seconds=sleep_for)
        print(f"Next run at {next_at:%Y-%m-%d %H:%M:%S}")
        time.sleep(sleep_for)
        run_once(config, args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HH.ru auto apply helper")
    parser.add_argument("--config", default=os.getenv("HH_CONFIG_PATH") or str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--state-db", default=os.getenv("HH_STATE_DB") or str(DEFAULT_STATE_DB))
    parser.add_argument("--once", action="store_true", help="Run one search/apply pass")
    parser.add_argument("--schedule", action="store_true", help="Run forever at schedule.run_times")
    parser.add_argument("--apply", action="store_true", help="Actually send responses. Default is dry-run.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless in --apply mode")
    parser.add_argument("--max-applications", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT_DIR / config_path
    config = load_yaml(config_path)

    try:
        load_llm_config()
    except RuntimeError as exc:
        print(f"{exc}. Create .env from .env.example.", file=sys.stderr)
        return 2

    if not args.once and not args.schedule:
        args.once = True

    if args.schedule:
        run_schedule(config, args)
    else:
        run_once(config, args)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
