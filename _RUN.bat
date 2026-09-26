@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist _TO_IMPORT.txt (
    echo # One Steam app id or store URL per line, then run _RUN.bat. Lines starting with # are ignored.> _TO_IMPORT.txt
    echo Created _TO_IMPORT.txt - fill it in and run _RUN.bat again.
    goto :end
)

uv run src/cli.py --file _TO_IMPORT.txt

:end
echo.
set /p "_=Press Enter to exit..." <con
