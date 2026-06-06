@echo off
cd /d "%~dp0"

if "%FDM_PGHOST%"=="" set FDM_PGHOST=127.0.0.1
if "%FDM_PGPORT%"=="" set FDM_PGPORT=5432
if "%FDM_PGDATABASE%"=="" set FDM_PGDATABASE=fdm_dashboard
if "%FDM_PGUSER%"=="" set FDM_PGUSER=postgres

if "%FDM_PGPASSWORD%"=="" (
    if exist "postgres_password.txt" (
        set /p FDM_PGPASSWORD=<postgres_password.txt
    ) else (
        set /p FDM_PGPASSWORD=Enter local PostgreSQL password:
    )
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m streamlit run fdm_dashboard.py --server.address 127.0.0.1 --server.port 8503
) else (
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
    python -m streamlit run fdm_dashboard.py --server.address 127.0.0.1 --server.port 8503
)
pause

