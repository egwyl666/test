@echo off
chcp 65001 >nul
title Prom Loader
cd /d "%~dp0"

echo.
echo  Prom Loader: запуск...
echo.

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto deps

rem ---------- 1. Ищем Python 3.10 или новее ----------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if defined PY goto venv

echo  Python не найден. Пробую установить его автоматически...
echo  Если Windows спросит разрешение - нажмите "Да".
echo.
where winget >nul 2>nul
if errorlevel 1 goto nopython
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto nopython
echo.
echo  ============================================================
echo   Python установлен.
echo   ЗАКРОЙТЕ это окно и снова дважды щёлкните по START.bat
echo  ============================================================
pause
exit /b 0

:nopython
echo.
echo  ============================================================
echo   Не получилось установить Python автоматически.
echo   Сейчас откроется сайт python.org:
echo    1. Нажмите жёлтую кнопку "Download Python".
echo    2. Запустите скачанный файл.
echo    3. ОБЯЗАТЕЛЬНО поставьте галочку "Add python.exe to PATH"
echo       внизу первого окна установки, затем "Install Now".
echo    4. После установки снова запустите START.bat
echo  ============================================================
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

rem ---------- 2. Отдельное окружение для программы ----------
:venv
echo  Первый запуск: готовлю программу. Это займёт 1-3 минуты, нужен интернет...
%PY% -m venv .venv
if errorlevel 1 goto venvfail

rem ---------- 3. Компоненты программы (только при первом запуске и после обновления) ----------
:deps
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>nul
if not errorlevel 1 goto run
echo  Устанавливаю компоненты программы...
"%VENV_PY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%VENV_PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto pipfail
copy /y requirements.txt ".venv\installed-requirements.txt" >nul

rem Ярлык "Prom Loader" на рабочем столе, чтобы в следующий раз не искать папку
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $p=Join-Path $d 'Prom Loader.lnk'; if(-not (Test-Path $p)){ $s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); $s.TargetPath='%~dp0START.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%SystemRoot%\System32\shell32.dll,13'; $s.Save() }" >nul 2>nul
echo  Готово! На рабочем столе появился ярлык "Prom Loader".
echo.

rem ---------- 4. Запуск ----------
:run
"%VENV_PY%" -m promloader.launcher
if errorlevel 1 pause
exit /b

:venvfail
echo.
echo  Не удалось подготовить Python для программы.
echo  Переустановите Python с python.org и запустите START.bat снова.
pause
exit /b 1

:pipfail
echo.
echo  ============================================================
echo   Не удалось скачать компоненты программы.
echo   Проверьте интернет и запустите START.bat ещё раз.
echo   Если не помогло - сделайте снимок этого окна и отправьте разработчику.
echo  ============================================================
pause
exit /b 1
