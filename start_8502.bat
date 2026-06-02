@echo off
cd /d "%~dp0"
if "%FDM_PGHOST%"=="" set FDM_PGHOST=localhost
if "%FDM_PGPORT%"=="" set FDM_PGPORT=2926
if "%FDM_PGDATABASE%"=="" set FDM_PGDATABASE=fdm_dashboard
if "%FDM_PGUSER%"=="" set FDM_PGUSER=postgres
if "%FDM_PGPASSWORD%"=="" set /p FDM_PGPASSWORD=Enter PostgreSQL password:
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m streamlit run fdm_dashboard.py --server.address 0.0.0.0 --server.port 8502
) else (
    python -m streamlit run fdm_dashboard.py --server.address 0.0.0.0 --server.port 8502
)
pause
