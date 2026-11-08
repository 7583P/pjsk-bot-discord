# Prueba OAuth real en Vercel Preview (sin bypass, sin sesiones falsas)

Alias: `https://pjsk-page-preview.vercel.app` · Callback: `https://pjsk-page-preview.vercel.app/auth02/callback`
La API usa el rol `lounge_api` (SSL, pooler transaccional puerto 6543). La protección de Vercel sigue activa.

## Acciones humanas
1. Discord Developer Portal → aplicación 1371546746248761455 → OAuth2 → Redirects → **añadir** exactamente la URI de callback de arriba (no quites las existentes). Guardar.
2. Abrir el alias en el navegador (inicia sesión en Vercel si lo pide) y pulsar "Login con Discord".
3. **Antes de autorizar**, comprobar en la URL de Discord que `redirect_uri` = la URI de callback exacta.
4. Autorizar. Debes volver al alias con la sesión iniciada.
5. En la consola del navegador (F12) del alias, pegar el script de abajo y copiar la tabla resultante.

## Script de consola (solo lectura)
Garantías: no lee ni imprime cookies, tokens, cabeceras `Authorization`, cuerpos de respuesta ni secretos; solo códigos HTTP, tiempos, conteos y el número de campos de `/auth02/me`. No cambia títulos ni cierra sesión.
```js
(async () => {
  const out = [], rec = (check, ok, detail = '') => out.push({ check, ok, detail });
  const j = async (p, o = {}) => { const t = performance.now(); const r = await fetch(p, { credentials: 'include', ...o }); let b = null; try { b = await r.clone().json(); } catch {} return { r, b, ms: Math.round(performance.now() - t) }; };
  let x = await j('/auth02/me'); rec('/auth02/me', x.r.status === 200, `${x.r.status} ${x.ms}ms fields=${x.b ? Object.keys(x.b).length : 0}`);
  x = await j('/auth02/pfps'); rec('/auth02/pfps', x.r.status === 200, `${x.r.status} ${x.ms}ms`);
  x = await j('/auth02/titles'); rec('/auth02/titles', x.r.status === 200, `${x.r.status} ${x.ms}ms available=${x.b?.available?.length} selected=${x.b?.selected?.key ?? null}`);
  for (const p of ['/api/seasons', '/api/players?limit=5', '/api/hall-of-fame', '/api/season-cutoffs', '/api/matches?limit=3']) {
    x = await j(p); rec(p, x.r.status === 200, `${x.r.status} ${x.ms}ms`); }
  const t0 = performance.now(); const burst = await Promise.all(Array.from({ length: 30 }, () => fetch('/api/seasons', { credentials: 'include' })));
  rec('30 concurrent /api/seasons', burst.every(r => r.status === 200), `${Math.round(performance.now() - t0)}ms total, codes=${[...new Set(burst.map(r => r.status))]}`);
  rec('page served over HTTPS', location.protocol === 'https:');
  rec('session cookie NOT readable from JS (HttpOnly)', !/pjsk|session/i.test(document.cookie));
  console.table(out);
})();
```

## Escrituras reales de tu propio perfil (opcionales, las haces tú en la UI)
- **Selección de título:** elige otro título y vuelve a poner el original.
- **Logout:** pulsa "Logout"; luego recarga y comprueba que `/auth02/me` da 401 y la cookie desaparece.

Después avísame (`python -m scripts.post_login_check` comprueba conexiones, errores del runtime y OAuth en el servidor): reviso los logs de runtime de Vercel y las conexiones en Supabase (rol `lounge_api`, sin errores de permisos).
