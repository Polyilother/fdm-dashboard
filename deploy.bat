@echo off
setlocal

cd /d "%~dp0"

echo [1/5] Pull latest code from Git...
git pull
if errorlevel 1 (
    echo Git pull failed. Please check network, branch, or repository permissions.
    pause
    exit /b 1
)

echo [2/5] Ensure Python virtual environment...
if not exist ".venv" (
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create .venv. Please check Python installation.
        pause
        exit /b 1
    )
)

echo [3/5] Activate virtual environment...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo Failed to activate .venv.
    pause
    exit /b 1
)

echo [4/5] Install dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

echo [5/5] Start FDM dashboard...
if "%FDM_PGHOST%"=="" set FDM_PGHOST=localhost
if "%FDM_PGPORT%"=="" set FDM_PGPORT=2926
if "%FDM_PGDATABASE%"=="" set FDM_PGDATABASE=fdm_dashboard
if "%FDM_PGUSER%"=="" set FDM_PGUSER=postgres
if "%FDM_PGPASSWORD%"=="" set /p FDM_PGPASSWORD=Enter PostgreSQL password:

python -m streamlit run fdm_dashboard.py --server.address 0.0.0.0 --server.port 8502

pause
