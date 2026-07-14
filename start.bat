@echo off
cd /d %~dp0

echo Starting backend on http://127.0.0.1:8000 ...
start "VOC Insight Agent - Backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"

echo Starting frontend on http://localhost:5173 ...
start "VOC Insight Agent - Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

echo Waiting for both servers to come up...
timeout /t 6 /nobreak >nul

start http://localhost:5173

echo Done. Two windows opened (Backend / Frontend) - keep them open while using the app.
echo Close this window any time; closing the Backend/Frontend windows stops those servers.
pause
