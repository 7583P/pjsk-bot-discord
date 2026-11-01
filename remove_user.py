"""Legacy destructive SQLite utility retired; imports have no side effects."""

if __name__ == "__main__":
    raise SystemExit(
        "La eliminación física de jugadores con historial está deshabilitada. Usa una migración administrativa auditada; no edites el SQLite histórico."
    )
