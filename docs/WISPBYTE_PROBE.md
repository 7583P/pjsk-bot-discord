# Probe en Wispbyte (sin Gateway de Discord)

ZIP: `D:\Projects\PJSK Bot\artifacts\wispbyte-package-20261108T205037Z.zip` (sha256 `6a6ba897a850b426ae2f2ef2d05c32aeac511d8bc3b4528b75befcbba3ca6a9e`).

1. Files → borrar `.pydeps/` si existe y el contenido anterior del bot → subir el ZIP → Unzip en la raíz (deben quedar `bootstrap.py`, `probe.py`, `main.py`, `run_bot.py`, `MANIFEST.json` en la raíz).
2. Startup → variable `DATABASE_URL` (ya configurada). Si el panel tiene variables de auto-instalación (p. ej. *Auto Update / Requirements / PY_PACKAGES*), ponlas vacías/desactivadas: `bootstrap.py` instala solo una vez en `.pydeps`.
3. **Startup command:** `python -u bootstrap.py probe.py`
4. Start. Verás `[bootstrap …] installing…` (≈1–3 min la primera vez), luego `PROBE COMPLETE`. Si se detiene, la última línea dice en qué paso (`WATCHDOG … stuck at step`) o pip falla con su código.
5. Copia **toda** la consola (no tiene secretos) y déjala OFFLINE.
6. Para el bot real (solo en el corte): Startup command `python -u bootstrap.py run_bot.py` (las siguientes arrancadas no reinstalan).
