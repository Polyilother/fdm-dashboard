@echo off
cd /d "%~dp0"

if not defined FDM_PGHOST set FDM_PGHOST=127.0.0.1
if not defined FDM_PGPORT (
    for /f %%P in ('powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; $r=$c.BeginConnect(''127.0.0.1'',2926,$null,$null); if($r.AsyncWaitHandle.WaitOne(300)){try{$c.EndConnect($r); ''2926''}catch{''5432''}}else{''5432''}; $c.Close()"') do set FDM_PGPORT=%%P
)
if not defined FDM_PGDATABASE set FDM_PGDATABASE=fdm_dashboard
if not defined FDM_PGUSER set FDM_PGUSER=postgres

if not defined FDM_PGPASSWORD (
    if exist "%~dp0postgres_password.txt" (
        set /p FDM_PGPASSWORD=<"%~dp0postgres_password.txt"
    )
)

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0jobs\sync_freshliance_env.py" --hours 72 --probe-type 0 --max-records 3 --loop --align-hour
) else (
    python "%~dp0jobs\sync_freshliance_env.py" --hours 72 --probe-type 0 --max-records 3 --loop --align-hour
)

pause
