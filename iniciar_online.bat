@echo off
chcp 65001 >nul
REM ============================================================
REM AGEPA online pelo seu PC: servidor (tela + IA) e túnel da Cloudflare.
REM Requisitos: .venv-gpu criado e cloudflared instalado:
REM   winget install --id Cloudflare.cloudflared
REM O link público (https://....trycloudflare.com) aparece nesta janela.
REM Fechar esta janela (ou Ctrl+C) tira o túnel do ar.
REM ============================================================
cd /d "%~dp0"
set "PORTA=8765"
set "CF=cloudflared"
where cloudflared >nul 2>&1 || set "CF=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if not exist "%CF%" if not "%CF%"=="cloudflared" set "CF=%ProgramFiles%\cloudflared\cloudflared.exe"
if not "%CF%"=="cloudflared" if not exist "%CF%" (
  echo cloudflared não encontrado. Instale com:  winget install --id Cloudflare.cloudflared
  pause & exit /b 1
)
set "AGEPA_FRONTEND_DIR=%~dp0frontend"
set PYTHONIOENCODING=utf-8
curl -s -m 2 http://127.0.0.1:%PORTA%/ >nul 2>&1 && goto tunel
echo Iniciando o servidor do AGEPA (porta %PORTA%)...
start "AGEPA servidor" /min cmd /c "cd /d "%~dp0backend" && "%~dp0.venv-gpu\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port %PORTA%"
echo Aguardando o servidor carregar os modelos...
:espera
timeout /t 3 >nul
curl -s -m 2 http://127.0.0.1:%PORTA%/ >nul 2>&1 || goto espera
:tunel
echo.
echo Servidor pronto. Abrindo o túnel: procure abaixo a linha com https://....trycloudflare.com
echo Envie esse link para quem for usar (ele muda a cada vez que o túnel é aberto).
echo.
"%CF%" tunnel --url http://127.0.0.1:%PORTA%
pause
