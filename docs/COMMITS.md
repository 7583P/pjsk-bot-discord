# Revisiones locales de preparación

Sin push, despliegue ni reset productivo.

| Repositorio | Rama | Commit local |
|---|---|---|
| Backend moderno | `dev/season5-current` | `3044f43` |
| Web | `dev/season5-web` | `36ca4c9` |
| Workspace histórico y documentación | `dev/season5-new-era` | Commit de conservación de esta entrega; consultar HEAD de la raíz |

Los dos primeros son repositorios/worktrees independientes dentro de carpetas ignoradas por la raíz. No eliminarlos. Sus paquetes de revisión no incluyen historial Git, entornos privados ni bases.

Las migraciones modernas se guardan sin normalización de finales de línea para mantener sus checksums exactos. Se verificó que cada blob SQL staged coincide con los bytes probados. El DDL capturado de upstream conserva su whitespace original mediante una excepción Git limitada a ese archivo.

Pruebas: 49 modernas, 11 web y 31 históricas de raíz. Las últimas no validan reglas competitivas modernas. El informe y la matriz de QA mantienen NOT READY hasta verificar integraciones reales.
