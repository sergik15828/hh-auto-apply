# hh-auto-apply

Автопоиск вакансий на hh.ru, генерация персонального сопроводительного письма и автоотклик через сохраненную браузерную сессию.

## Что уже умеет

- Ищет вакансии через публичный API hh.ru.
- Ограничивает поиск ключевыми словами в названии вакансии через `search_field=name`.
- Берет данные о кандидате из `my/profile.md` или PDF-резюме в `my/`.
- Генерирует сопроводительное письмо через OpenAI или Anthropic API.
- Ведет SQLite-журнал обработанных вакансий, чтобы не откликаться повторно.
- По умолчанию работает в безопасном `dry-run`; реальные отклики отправляются только с флагом `--apply`.
- Может запускаться 2 раза в день по расписанию из `my/config.yaml`.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
mkdir -p my
cp .env.example .env
cp config.example.yaml my/config.yaml
cp examples/profile.example.md my/profile.md
cp examples/cover_letter_prompt.example.md my/cover_letter_prompt.md
```

Заполни `.env`. Для OpenAI:

```dotenv
LLM_PROVIDER=openai
HH_USER_AGENT=hh-auto-apply/1.0 (your-email@example.com)
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

Для Anthropic:

```dotenv
LLM_PROVIDER=anthropic
HH_USER_AGENT=hh-auto-apply/1.0 (your-email@example.com)
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6
```

Папка `my/` игнорируется git'ом, поэтому там можно хранить личные данные:

```text
my/
  config.yaml
  profile.md        # предпочтительно
  cover_letter_prompt.md
  cv-v2-ru.pdf      # fallback, если profile.md нет
```

`profile.md` лучше сделать коротким и фактическим: кто ты, стек, опыт, сильные кейсы, портфолио, формат работы. Если файла нет, скрипт попробует извлечь текст из PDF через `pypdf`.

Шаблоны приватных файлов лежат в `examples/` и безопасны для git:

- `examples/profile.example.md`
- `examples/cover_letter_prompt.example.md`

## Авторизация hh.ru

Один раз сохрани браузерную сессию:

```bash
source .venv/bin/activate
python3 hh_login.py
```

Скрипт откроет браузер. Войди на hh.ru, дождись личного кабинета и нажми Enter в терминале.

## Первый запуск

Сначала dry-run, без реальных откликов:

```bash
source .venv/bin/activate
python3 auto_apply.py --once
```

Скрипт найдет вакансии, сгенерирует письма и запишет результат в `data/hh_auto_apply.sqlite3`, но ничего не отправит.

Когда письма и фильтры устраивают:

```bash
python3 auto_apply.py --once --apply
```

Ограничить число откликов:

```bash
python3 auto_apply.py --once --apply --max-applications 2
```

## Запуск 2 раза в день

В `my/config.yaml` настрой:

```yaml
schedule:
  run_times:
    - "09:30"
    - "18:30"
```

Запусти долгоживущий процесс:

```bash
python3 auto_apply.py --schedule --apply
```

Для надежной ежедневной работы лучше потом завернуть эту команду в `systemd` user service или cron.

## Важные настройки

Верхний блок `vacancies` в `my/config.yaml`:

- `keywords` - ключевые слова для поиска вакансий по названию.
- `required_title_words_any` - строгий локальный фильтр: оставить вакансию, только если в названии есть хотя бы одно из этих слов.
- `skip_already_applied: true` - не брать вакансии, которые уже успешно обработаны в журнале.
- `stop_words` - стоп-слова в названии вакансии: например `стажер`, `junior`.
- `remote_only: true` - искать только удаленный формат и исключать офисные вакансии.
- `application_questions` - ответы на частые дополнительные вопросы работодателя, например город и зарплатные ожидания.

Технические настройки ниже:

- `search.title_only: true` - искать по названию вакансии.
- `filters.skip_has_test: true` - пропускать вакансии с тестовым заданием.
- `limits.max_applications_per_run` - потолок откликов за один запуск.
- `letter.prompt_path` - путь к редактируемому промпту для сопроводительных писем.
- `letter.extra_instructions` - стиль и правила сопроводительного письма.

Промпт для писем можно править в `my/cover_letter_prompt.md`.

## Структура

- `auto_apply.py` - основной скрипт.
- `hh_login.py` - сохранение сессии hh.ru.
- `config.example.yaml` - пример персонального конфига.
- `.env.example` - пример переменных окружения.
- `examples/` - безопасные шаблоны приватных файлов.
- `requirements.txt` - зависимости.
