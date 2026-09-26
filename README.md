# steam-art-scraper

Скачивает арт Steam-приложений: капсулы, хедер, хиро, арт библиотеки, лого, фон страницы,
скриншоты. Видео и трейлеры пропускаются.

## Быстрый старт (Windows)

1. Установите [uv](https://docs.astral.sh/uv/getting-started/installation/) — в PowerShell:
   `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`.
2. Откройте `_TO_IMPORT.txt` и впишите игры по одной на строку — app id или ссылку на страницу
   в Steam:

   ```text
   367520
   https://store.steampowered.com/app/1030300/Hollow_Knight_Silksong/
   ```

   Строки, начинающиеся с `#`, игнорируются.
3. Запустите `_RUN.bat` двойным кликом.
4. Арт появится в папке `output`.

## Командная строка

```sh
uv run src/cli.py 367520 https://store.steampowered.com/app/1030300/ -o output
uv run src/cli.py --file _TO_IMPORT.txt
uv run src/cli.py 367520 --list   # только вывести URL
```

Каждое приложение сохраняется в `<out>/<ГГГГММДД ЧЧММСС> - <Название>`.

## Разработка

Нужен [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run prek install
```

Хук запускает `ruff format`, `ruff check --fix`, `pyrefly check`, `pytest` и проверяет, что в
`_TO_IMPORT.txt` нет записей (только комментарии). Запустить вручную на всех файлах:

```sh
uv run prek run --all-files
```
