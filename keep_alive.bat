@echo off
:RESTART
echo [%time%] Arrancando el bot...
python main.py
echo [%time%] Bot detenido. Reiniciando en 5 segundos...
timeout /t 5 /nobreak >nul
goto RESTART
