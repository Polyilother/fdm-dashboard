@echo off
cd /d "%~dp0"
streamlit run fdm_dashboard.py --server.address 0.0.0.0 --server.port 8501
pause
