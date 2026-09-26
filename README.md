# steam-art-scraper

Скачивает арт Steam-приложений: капсулы, хедер, хиро, арт библиотеки, лого, фон страницы,
скриншоты. Видео и трейлеры пропускаются.

## Использование

```sh
uv run src/cli.py 367520 https://store.steampowered.com/app/1030300/ -o output
uv run src/cli.py 367520 --list   # только вывести URL
```

Каждое приложение сохраняется в `<out>/<app_id> <Название>`, например `output/367520 Hollow_Knight/`.

## Разработка

Нужен [uv](https://docs.astral.sh/uv/).

```sh
uv sync                  # venv на Python 3.14 + зависимости, включая dev-инструменты (prek, ruff, pyrefly, pytest)
uv run prek install      # один раз: установить git pre-commit хук
```

Хук запускает `ruff format`, `ruff check --fix`, `pyrefly check` и `pytest`.
Запустить вручную на всех файлах:

```sh
uv run prek run --all-files
```
