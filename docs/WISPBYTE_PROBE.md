# Probe en Wispbyte (sin Gateway de Discord)

ZIP: `D:\Projects\PJSK Bot\artifacts\wispbyte-package-20261108T183833Z.zip` (11.7 MB, sha256 `96b917ae0aa254ee6daf552a7dfaa909e32e79e24ffeb4dd55ba2c21c44dc5df`).

1. Servidor `discord_bot` → **Files** → subir el ZIP a la raíz → clic derecho → **Unzip**. Debe quedar `probe.py`, `main.py`, `run_bot.py`, `requirements.txt` en la raíz (no en una subcarpeta).
2. **Startup** → variables: `DATABASE_URL` = el mismo valor que usa Railway (pooler *session*, puerto 5432). No añadas `DISCORD_TOKEN`.
3. **Startup command**: `python probe.py` (el panel instalará `requirements.txt`; si no lo hace, consola: `pip install -r requirements.txt`).
4. **Start**. Espera a que termine (≈20 s) y copia la salida completa de la consola (no contiene secretos).
5. Para medir CPU/RAM del bot real sin Gateway: Startup command `python -c "import main"` ≈ imports y pool; el consumo se ve en la gráfica del panel.
6. Cuando termines, deja el servidor **OFFLINE**. No uses `run_bot.py` ni `main.py` hasta el corte.
