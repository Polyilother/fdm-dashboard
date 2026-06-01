@echo off
cd /d "%~dp0"
set FDM_PGHOST=localhost
set FDM_PGPORT=5432
set FDM_PGDATABASE=fdm_dashboard
set FDM_PGUSER=postgres
if "%FDM_PGPASSWORD%"=="" set /p FDM_PGPASSWORD=Enter PostgreSQL password:
"C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe" -m streamlit run fdm_dashboard.py --server.address 0.0.0.0 --server.port 8502
pause
