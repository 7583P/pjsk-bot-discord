# PJSK LOUNGE — Season 5 (New Era)

> **Actualización 2026-11-01:** el backend moderno auténtico fue recuperado. El candidato vigente está en `.local/season5-current/` (`dev/season5-current`) y la web en `web/` (`dev/season5-web`). Consulta [el informe moderno](docs/REPORT.md) y [su procedimiento](docs/CURRENT_DEPLOYMENT.md). Las instrucciones y evidencia anteriores que siguen debajo describen el prototipo 2025 conservado; no ejecutarlas como migración productiva moderna. Estado: **NOT READY**, sin reset productivo.

Bot Discord Python, PostgreSQL/Supabase y leaderboard público FastAPI preparado para Vercel. Se conserva la fórmula competitiva disponible en este repositorio.

**Estado: NOT READY para lanzamiento productivo.** Implementación y ensayo local verificados; faltan Supabase/hosting/Vercel reales y validación de Discord. El token actual aparece en el historial Git: debe rotarse. El arranque rechaza los hashes de los tokens históricos conocidos.

- [Informe de entrega](docs/REPORT.md).
- [Inventario, reglas originales y causas raíz](docs/AUDIT.md).
- [Evidencia del ensayo con 58 jugadores](docs/EVIDENCE.json).
- [Instalación, migración, activación y rollback](docs/DEPLOYMENT.md).
- [Hosting gratuito y limitaciones](docs/HOSTING.md).

Requisitos: Python 3.13, PostgreSQL 15+; Node 22 para verificar assets. No hay SQLite en runtime ni un segundo Gateway Node. No se ejecutan migraciones ni resets al arrancar.

```sh
python -m venv .venv
# Linux: source .venv/bin/activate
# Windows: .venv\Scripts\python.exe en lugar de python
python -m pip install -r requirements.lock
# Configurar variables según .env.example; no publicar .env.
python -m scripts.migrate
# Importar un backup verificado antes de arrancar: ver docs/DEPLOYMENT.md.
python main.py
# API local, otro proceso y API_DATABASE_URL de solo lectura:
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Verificación, con un servidor PostgreSQL local aislado:

```sh
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m mypy
# TEST_DATABASE_ADMIN_URL: servidor LOCAL para crear bases pjsk_test_*.
# PG_BIN_DIR: directorio de pg_dump/pg_restore compatibles.
python -m scripts.test_suite
python -m scripts.security_check
python -m pip_audit -r requirements.lock
npm run build
```

Sin TEST_DATABASE_ADMIN_URL las pruebas de integración se omiten explícitamente. Sin PG_BIN_DIR se omite la restauración dentro de la suite. Las evidencias de esta entrega incluyen ambas verificaciones.
