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
from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT_DIR = Path(__file__).resolve().parent
MY_DIR = ROOT_DIR / "my"
DEFAULT_CONFIG_PATH = MY_DIR / "config.yaml"
DEFAULT_STATE_DB = ROOT_DIR / "data" / "hh_auto_apply.sqlite3"
SKIPPED_LOG_PATH = ROOT_DIR / "data" / "skipped_vacancies.txt"
DEFAULT_COVER_LETTER_PROMPT_PATH = MY_DIR / "cover_letter_prompt.md"
HH_API_BASE = "https://api.hh.ru"
DEFAULT_HH_WEB_BASE = "https://rostov.hh.ru"
DEFAULT_USER_AGENT = "hh-auto-apply/1.0 (contact: set HH_USER_AGENT in .env)"
DEFAULT_HH_API_TIMEOUT_SECONDS = 45
DEFAULT_HH_API_RETRIES = 3
DEFAULT_HH_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
DEFAULT_HH_API_REFERER = "https://hh.ru/"
DEFAULT_HH_BROWSER_NAV_RETRIES = 3
DEFAULT_HH_BROWSER_NAV_TIMEOUT_SECONDS = 60
DEFAULT_LLM_RETRIES = 3
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
RETRYABLE_HH_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

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
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = split_letter_paragraphs(value)
    if not paragraphs:
        return ""
    if len(paragraphs) == 1:
        paragraphs = paragraphize_sentences(paragraphs[0])
    return "\n\n".join(paragraphs).strip()


def split_letter_paragraphs(value: str) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for raw_line in value.split("\n"):
        line = normalize_space(raw_line)
        if line:
            current.append(line)
            continue
        if current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def paragraphize_sentences(value: str) -> list[str]:
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", value) if sentence.strip()]
    if len(sentences) < 3:
        return [value]
    if len(sentences) == 3:
        return sentences
    return [sentences[0], " ".join(sentences[1:-1]), sentences[-1]]


def validate_letter_policy(letter: str, config: dict[str, Any]) -> None:
    if "ё" in letter or "Ё" in letter:
        raise ValueError('Letter contains forbidden "ё" character')
    if any(char in letter for char in ("—", "–", "−", "‑")):
        raise ValueError("Letter contains forbidden long dash character")

    forbidden_terms = [
        str(value).strip()
        for value in get_nested(config, "letter.forbidden_terms", [])
        if str(value).strip()
    ]
    low_letter = letter.lower()
    for term in forbidden_terms:
        if term.lower() in low_letter:
            raise ValueError(f"Letter contains forbidden term from letter.forbidden_terms: {term}")

    question_config = get_nested(config, "application_questions", {})
    application_only_values = [
        str(question_config.get("city") or "").strip(),
        str(question_config.get("salary_expectations") or "").strip(),
    ]
    for item in question_config.get("answers") or []:
        if isinstance(item, dict):
            application_only_values.append(str(item.get("answer") or "").strip())

    for value in application_only_values:
        if value and value.lower() in low_letter:
            raise ValueError(
                "Letter contains application question answer that must be used only in explicit question fields"
            )


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
    retries = hh_api_retries()
    timeout = hh_api_timeout_seconds()
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            headers=hh_api_headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_HH_HTTP_CODES and attempt < retries:
                last_error = exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after and retry_after.isdigit():
                    wait_seconds = max(1, int(retry_after))
                else:
                    wait_seconds = 2 * attempt
                print(
                    f"HH API {exc.code}, retry {attempt}/{retries} after {wait_seconds}s: {url}",
                    flush=True,
                )
                time.sleep(wait_seconds)
                continue
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HH API error {exc.code}: {body[:500]}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt < retries:
                print(f"HH API timeout/network error, retry {attempt}/{retries}: {url}", flush=True)
                time.sleep(2 * attempt)

    raise RuntimeError(f"HH API network error after {retries} attempts: {last_error}") from last_error


def hh_user_agent() -> str:
    return os.getenv("HH_USER_AGENT") or DEFAULT_USER_AGENT


def hh_browser_user_agent() -> str:
    return os.getenv("HH_BROWSER_USER_AGENT") or DEFAULT_BROWSER_USER_AGENT


def hh_api_headers() -> dict[str, str]:
    headers = {
        "User-Agent": hh_browser_user_agent() if hh_browser_headers_enabled() else hh_user_agent(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": os.getenv("HH_ACCEPT_LANGUAGE") or DEFAULT_HH_ACCEPT_LANGUAGE,
        "Referer": os.getenv("HH_API_REFERER") or DEFAULT_HH_API_REFERER,
        "Connection": "close",
    }

    # Keep the app/contact identifier available when using a browser-like UA.
    if hh_browser_headers_enabled() and hh_user_agent():
        headers["HH-User-Agent"] = hh_user_agent()

    return headers


def hh_browser_headers_enabled() -> bool:
    return (os.getenv("HH_BROWSER_HEADERS") or "true").strip().lower() not in ("0", "false", "no")


def hh_api_timeout_seconds() -> int:
    return int(os.getenv("HH_API_TIMEOUT_SECONDS") or DEFAULT_HH_API_TIMEOUT_SECONDS)


def hh_api_retries() -> int:
    return int(os.getenv("HH_API_RETRIES") or DEFAULT_HH_API_RETRIES)


def llm_retries() -> int:
    return int(os.getenv("LLM_RETRIES") or DEFAULT_LLM_RETRIES)


def hh_browser_nav_retries() -> int:
    return int(os.getenv("HH_BROWSER_NAV_RETRIES") or DEFAULT_HH_BROWSER_NAV_RETRIES)


def hh_browser_nav_timeout_ms() -> int:
    seconds = int(os.getenv("HH_BROWSER_NAV_TIMEOUT_SECONDS") or DEFAULT_HH_BROWSER_NAV_TIMEOUT_SECONDS)
    return seconds * 1000


def hh_web_base() -> str:
    return (os.getenv("HH_WEB_BASE") or DEFAULT_HH_WEB_BASE).rstrip("/")


def launch_hh_browser(playwright: Any, headless: bool):
    options: dict[str, Any] = {
        "headless": headless,
        "slow_mo": 250,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--start-maximized",
        ],
    }
    channel = (os.getenv("HH_BROWSER_CHANNEL") or "").strip()
    if channel:
        options["channel"] = channel
    return playwright.chromium.launch(**options)


def new_hh_context(browser: Any, state_path: Path | None = None):
    options: dict[str, Any] = {
        "user_agent": hh_browser_user_agent(),
        "locale": "ru-RU",
        "timezone_id": os.getenv("TZ") or "Europe/Moscow",
        "viewport": {"width": 1365, "height": 900},
        "extra_http_headers": {
            "Accept-Language": os.getenv("HH_ACCEPT_LANGUAGE") or DEFAULT_HH_ACCEPT_LANGUAGE,
        },
    }
    if state_path is not None and state_path.exists():
        options["storage_state"] = str(state_path)
    return browser.new_context(**options)


def goto_hh_page(page: Page, url: str, label: str) -> bool:
    retries = hh_browser_nav_retries()
    timeout_ms = hh_browser_nav_timeout_ms()
    for attempt in range(1, retries + 1):
        try:
            print(f"Opening hh.ru {label}: attempt {attempt}/{retries}", flush=True)
            page.goto(url, wait_until="commit", timeout=timeout_ms)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1500)
            if not page_is_browser_error(page):
                return True
            print(f"Browser loaded an error page for hh.ru {label}: {page.title()}", flush=True)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            print(f"Browser {label} page load failed: {exc}", flush=True)

        if attempt < retries:
            page.wait_for_timeout(2500 * attempt)

    return False


def page_is_browser_error(page: Page) -> bool:
    try:
        title = page.title()
    except Exception:
        title = ""
    heading = first_visible_text(page, ["h1"])
    return browser_error_page(heading, title)


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


def vacancy_from_details(details: dict[str, Any], query: str = "manual") -> Vacancy:
    vacancy_id = str(details.get("id") or "")
    if not vacancy_id:
        raise ValueError("Vacancy details do not contain id")

    schedule = details.get("schedule") or {}
    return Vacancy(
        id=vacancy_id,
        title=normalize_space(str(details.get("name") or "")),
        employer=normalize_space(str((details.get("employer") or {}).get("name") or "")) or "Компания",
        url=str(details.get("alternate_url") or ""),
        apply_url=str(details.get("apply_alternate_url") or details.get("alternate_url") or ""),
        description=strip_html(str(details.get("description") or "")),
        has_test=bool(details.get("has_test")),
        response_letter_required=bool(details.get("response_letter_required")),
        query=query,
        schedule_id=str(schedule.get("id") or ""),
        schedule_name=str(schedule.get("name") or ""),
    )


def vacancy_from_url(url: str) -> Vacancy:
    vacancy_id = extract_vacancy_id(url)
    if not vacancy_id:
        raise ValueError(f"Could not extract vacancy id from URL: {url}")
    return vacancy_from_details(fetch_vacancy_details(vacancy_id), query="manual-url")


def vacancy_from_url_browser(url: str, headless: bool = False) -> Vacancy:
    vacancy_id = extract_vacancy_id(url)
    if not vacancy_id:
        raise ValueError(f"Could not extract vacancy id from URL: {url}")

    state_path = session_file()
    with sync_playwright() as p:
        browser = launch_hh_browser(p, headless=headless)
        context = new_hh_context(browser, state_path)
        page = context.new_page()
        try:
            if not goto_hh_page(page, url, "vacancy"):
                raise RuntimeError(f"Browser could not load hh.ru vacancy page: {url}")
            page.wait_for_timeout(1500)
            title = first_visible_text(page, ["[data-qa='vacancy-title']", "h1"]) or f"Vacancy {vacancy_id}"
            employer = (
                first_visible_text(
                    page,
                    [
                        "[data-qa='vacancy-company-name']",
                        "[data-qa='vacancy-company-name'] a",
                        "[data-qa='bloko-header-2']",
                    ],
                )
                or "Компания"
            )
            description, _ = get_vacancy_description_browser(page, url)
            page_title = page.title()
            if browser_error_page(title, page_title) or (title == f"Vacancy {vacancy_id}" and not description):
                raise RuntimeError(f"Browser could not load hh.ru vacancy page: {url}")
            return Vacancy(
                id=vacancy_id,
                title=title,
                employer=employer,
                url=url,
                apply_url=url,
                description=description,
                has_test=False,
                response_letter_required=False,
                query="manual-url-browser",
                schedule_id="",
                schedule_name="",
            )
        finally:
            context.close()
            browser.close()


def first_visible_text(page: Page, selectors: list[str]) -> str:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0 and locator.is_visible(timeout=1000):
                return normalize_space(locator.inner_text(timeout=1000))
        except Exception:
            continue
    return ""


def browser_error_page(title: str, page_title: str) -> bool:
    combined = f"{title} {page_title}".lower()
    error_markers = [
        "this site can't be reached",
        "this site can’t be reached",
        "не удается получить доступ",
        "не удаётся получить доступ",
        "err_timed_out",
        "err_connection",
    ]
    return any(marker in combined for marker in error_markers)


def load_manual_vacancy(url: str, allow_browser_fallback: bool, headless: bool) -> Vacancy:
    try:
        return vacancy_from_url(url)
    except RuntimeError:
        if not allow_browser_fallback:
            raise
        print("HH API vacancy fetch failed. Falling back to browser vacancy page...", flush=True)
        return vacancy_from_url_browser(url, headless=headless)


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


def search_vacancies(
    config: dict[str, Any],
    conn: sqlite3.Connection | None = None,
    allow_browser_fallback: bool = True,
    headless: bool = False,
) -> list[Vacancy]:
    try:
        return search_vacancies_api(config, conn)
    except RuntimeError as exc:
        if not allow_browser_fallback:
            raise
        print(f"HH API search failed: {exc}")
        print("Falling back to hh.ru browser search...")
        state_path = session_file()
        if not state_path.exists():
            raise RuntimeError(f"HH session not found: {state_path}. Run python3 hh_login.py") from exc
        with sync_playwright() as p:
            browser = launch_hh_browser(p, headless=headless)
            context = new_hh_context(browser, state_path)
            page = context.new_page()
            try:
                return search_vacancies_browser(page, config, conn)
            finally:
                context.close()
                browser.close()


def search_vacancies_api(
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
    remote_only = remote_only_enabled(config)

    for raw_query in queries:
        query = str(raw_query).strip()
        if not query:
            continue

        for page_num in range(max_pages):
            print(f"Searching hh.ru API: {query} (page {page_num + 1}/{max_pages})", flush=True)
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

                item_title = normalize_space(str(item.get("name") or ""))
                item_employer = normalize_space(str((item.get("employer") or {}).get("name") or ""))
                item_url = normalize_space(str(item.get("alternate_url") or ""))
                item_schedule_id = str((item.get("schedule") or {}).get("id") or "")
                item_has_test = bool(item.get("has_test"))

                stop_match = keyword_match(item_title, vacancy_stop_words(config))
                if stop_match:
                    if conn is not None:
                        record_result(
                            conn,
                            Vacancy(
                                id=vacancy_id,
                                title=item_title,
                                employer=item_employer or "Компания",
                                url=item_url,
                                apply_url=item_url,
                                description="",
                                has_test=item_has_test,
                                response_letter_required=False,
                                query=query,
                                schedule_id=item_schedule_id,
                                schedule_name="",
                            ),
                            "skipped",
                            f"Stop word in title: {stop_match}",
                            "",
                        )
                    continue

                # Cheap pre-filter using search response fields, so we skip the
                # per-vacancy details fetch for vacancies that already lose on
                # title/employer/schedule/has_test.
                if not vacancy_passes_filters(
                    config,
                    title=item_title,
                    employer=item_employer,
                    description="",
                    schedule_id=item_schedule_id,
                    has_test=item_has_test,
                ):
                    continue

                details = fetch_vacancy_details(vacancy_id)
                vacancy = vacancy_from_details(details, query=query)

                if not vacancy_passes_filters(
                    config,
                    title=vacancy.title,
                    employer=vacancy.employer,
                    description=vacancy.description,
                    schedule_id=vacancy.schedule_id or item_schedule_id,
                    has_test=vacancy.has_test,
                ):
                    continue

                results.append(vacancy)

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

            url = f"{hh_web_base()}/search/vacancy?" + urllib.parse.urlencode(params)
            if not goto_hh_page(page, url, "search"):
                print(f"Skipping browser search page after load failures: {url}", flush=True)
                continue
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
                stop_match = keyword_match(title, vacancy_stop_words(config))
                if stop_match:
                    if conn is not None:
                        record_result(
                            conn,
                            Vacancy(
                                id=vacancy_id,
                                title=title,
                                employer=employer or "Компания",
                                url=vacancy_url,
                                apply_url=vacancy_url,
                                description="",
                                has_test=False,
                                response_letter_required=False,
                                query=query,
                                schedule_id="remote" if remote_only_enabled(config) else "",
                                schedule_name="Удаленная работа" if remote_only_enabled(config) else "",
                            ),
                            "skipped",
                            f"Stop word in title: {stop_match}",
                            "",
                        )
                    continue
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
        if not goto_hh_page(page, url, "vacancy details"):
            return "", False
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


def append_skipped_log(vacancy: Vacancy, reason: str) -> None:
    SKIPPED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    line = (
        f"[{ts}] {reason} | {vacancy.title} | {vacancy.employer} | "
        f"{vacancy.url or vacancy.apply_url}\n"
    )
    with SKIPPED_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line)


def record_result(
    conn: sqlite3.Connection,
    vacancy: Vacancy,
    status: str,
    reason: str,
    letter: str,
) -> None:
    if status == "skipped":
        append_skipped_log(vacancy, reason)
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
- 3 абзаца, пустая строка между абзацами.
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

    user += """

Жесткое ограничение:
- Не указывай в сопроводительном письме город, формат работы и зарплатные ожидания.
- Не добавляй ответы из application_questions в текст письма.
- Эти данные заполняются только в отдельных вопросах работодателя, если такие поля есть на форме отклика.
"""

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
        letter = normalize_letter_text(generate_fallback_letter(vacancy))
        validate_letter_policy(letter, config)
        return letter

    system, user, max_chars = build_cover_letter_prompt(profile, vacancy, config)
    if llm.provider == "openai":
        letter = generate_openai_letter(llm, system, user)
    elif llm.provider == "anthropic":
        letter = generate_anthropic_letter(llm, system, user)
    else:
        raise RuntimeError(f"Unsupported LLM provider: {llm.provider}")
    letter = normalize_letter_text(letter)[:max_chars].strip()
    validate_letter_policy(letter, config)
    return letter


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


def _llm_call_with_retries(call, retryable_exceptions: tuple, label: str):
    retries = llm_retries()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return call()
        except retryable_exceptions as exc:
            last_error = exc
            if attempt < retries:
                wait_seconds = 2 ** attempt
                print(
                    f"{label} transient error, retry {attempt}/{retries} after {wait_seconds}s: {exc}",
                    flush=True,
                )
                time.sleep(wait_seconds)
    raise RuntimeError(f"{label} failed after {retries} attempts: {last_error}") from last_error


def generate_openai_letter(llm: LlmConfig, system: str, user: str) -> str:
    from openai import (
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        OpenAI,
        RateLimitError,
    )

    client = OpenAI(api_key=llm.api_key)

    def call():
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

    return _llm_call_with_retries(
        call,
        (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError),
        "OpenAI letter generation",
    )


def generate_anthropic_letter(llm: LlmConfig, system: str, user: str) -> str:
    from anthropic import (
        Anthropic,
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )

    client = Anthropic(api_key=llm.api_key)
    retryable = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)

    def call(model: str) -> str:
        response = client.messages.create(
            model=model,
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

    try:
        return _llm_call_with_retries(
            lambda: call(llm.model),
            retryable,
            f"Anthropic letter generation ({llm.model})",
        )
    except RuntimeError as primary_exc:
        fallback = (os.getenv("ANTHROPIC_FALLBACK_MODEL") or "").strip()
        if not fallback or fallback == llm.model:
            raise
        print(
            f"Anthropic primary model {llm.model} exhausted, trying fallback {fallback} (one attempt)",
            flush=True,
        )
        try:
            return call(fallback)
        except retryable as fb_exc:
            raise RuntimeError(
                f"Anthropic letter generation failed on primary {llm.model} "
                f"and fallback {fallback}: {fb_exc}"
            ) from fb_exc


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


def apply_to_vacancy(
    page: Page,
    vacancy: Vacancy,
    letter: str,
    config: dict[str, Any],
    confirm_before_submit: bool = False,
) -> tuple[str, str]:
    target_url = vacancy.url or vacancy.apply_url
    if not target_url:
        return "error", "No apply URL"

    if not goto_hh_page(page, target_url, "apply"):
        debug_base = save_apply_debug(page, vacancy.id)
        return "error", f"Could not load vacancy apply page; debug saved: {debug_base}"
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

    if confirm_before_submit:
        if not sys.stdin.isatty():
            debug_base = save_apply_debug(page, vacancy.id)
            return "error", f"Cannot confirm submit without TTY; debug saved: {debug_base}"
        print("\nReady to send real hh.ru response.")
        print(f"Vacancy: {vacancy.title} | {vacancy.employer}")
        print(f"URL: {vacancy.url or vacancy.apply_url}")
        answer = input('Type "send" to click the final submit button, anything else to skip: ').strip()
        if answer != "send":
            return "cancelled", "User skipped before final submit"

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
        return (
            "skipped",
            f"Submit button unavailable (extra form); debug saved: {debug_base}",
        )

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
    if args.vacancy_url:
        vacancies = [
            load_manual_vacancy(
                args.vacancy_url,
                allow_browser_fallback=not args.no_browser_fallback,
                headless=bool(args.headless),
            )
        ]
    else:
        vacancies = search_vacancies(
            config,
            conn,
            allow_browser_fallback=not args.no_browser_fallback,
            headless=bool(args.headless),
        )
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
            browser = launch_hh_browser(playwright, headless=bool(args.headless))
            browser_context = new_hh_context(browser, state_path)
            page = browser_context.new_page()

        for vacancy in vacancies:
            if max_per_run > 0 and sent_or_planned >= max_per_run:
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
                status, reason = apply_to_vacancy(
                    page,
                    vacancy,
                    letter,
                    config,
                    confirm_before_submit=bool(args.confirm_submit),
                )
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
        try:
            run_once(config, args)
        except Exception as exc:
            import traceback
            print(f"Scheduled run failed, continuing schedule: {exc}", file=sys.stderr, flush=True)
            traceback.print_exc()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HH.ru auto apply helper")
    parser.add_argument("--config", default=os.getenv("HH_CONFIG_PATH") or str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--state-db", default=os.getenv("HH_STATE_DB") or str(DEFAULT_STATE_DB))
    parser.add_argument("--once", action="store_true", help="Run one search/apply pass")
    parser.add_argument("--schedule", action="store_true", help="Run forever at schedule.run_times")
    parser.add_argument("--apply", action="store_true", help="Actually send responses. Default is dry-run.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless in --apply mode")
    parser.add_argument("--max-applications", type=int, default=None)
    parser.add_argument("--vacancy-url", default="", help="Process one exact hh.ru vacancy URL instead of search")
    parser.add_argument(
        "--no-browser-fallback",
        action="store_true",
        help="Fail on HH API search errors instead of opening browser fallback",
    )
    parser.add_argument(
        "--confirm-submit",
        action="store_true",
        help='In --apply mode, pause before final submit and require typing "send"',
    )
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
        try:
            run_schedule(config, args)
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    else:
        try:
            run_once(config, args)
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
