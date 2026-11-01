'use strict';
const byId = id => document.getElementById(id);
let offset = 0;
let generation = 0;
let debounce;
let pending;
const limit = 100;

async function refresh() {
  const current = ++generation;
  pending?.abort();
  pending = new AbortController();
  const timer = setTimeout(() => pending.abort(), 10000);
  try {
    const signal = pending.signal;
    const query = new URLSearchParams({limit, offset, q: byId('search').value});
    // Pin both reads to the same explicit season, even if a launch happens between requests.
    const seasonResponse = await fetch('/api/season', {signal});
    if (!seasonResponse.ok) throw new Error('No se pudo consultar la temporada.');
    const season = await seasonResponse.json();
    query.set('season', season.id);
    const response = await fetch(`/api/players?${query}`, {signal});
    if (!response.ok) throw new Error('No se pudo consultar la clasificación.');
    const players = await response.json();
    if (current !== generation) return;
    byId('season').textContent = season.name;
    byId('season-date').textContent = season.started_at
      ? `Inicio: ${new Intl.DateTimeFormat('es-PE', {timeZone: 'America/Lima', dateStyle: 'medium', timeStyle: 'short'}).format(new Date(season.started_at))} (Lima)`
      : '';
    const rows = players.map(player => {
      const row = document.createElement('tr');
      for (const value of [player.position, player.name, player.mmr, player.rank]) {
        const cell = document.createElement('td');
        cell.textContent = String(value);
        if (['Placement', 'Bronze', 'Gold', 'Diamond'].includes(player.rank)) cell.className = `rank-${player.rank}`;
        row.append(cell);
      }
      if (/^[A-Z]{2}$/i.test(player.country)) {
        const flag = document.createElement('img');
        flag.src = `https://flagcdn.com/24x18/${player.country.toLowerCase()}.png`;
        flag.alt = player.country;
        flag.className = 'flag';
        flag.loading = 'lazy';
        row.firstChild.append(flag);
      }
      return row;
    });
    byId('players').replaceChildren(...rows);
    byId('status').textContent = players.length ? 'Clasificación actualizada.' : 'No hay jugadores para esta búsqueda o temporada.';
    byId('previous').disabled = offset === 0;
    byId('next').disabled = players.length < limit;
    byId('page').textContent = `Página ${offset / limit + 1}`;
  } catch (error) {
    if (current === generation) byId('status').textContent = 'No se pudieron actualizar los datos. Se reintentará automáticamente.';
  } finally {
    clearTimeout(timer);
  }
}
byId('search').addEventListener('input', () => {
  clearTimeout(debounce);
  debounce = setTimeout(() => { offset = 0; refresh(); }, 250);
});
byId('previous').addEventListener('click', () => { offset = Math.max(0, offset - limit); refresh(); });
byId('next').addEventListener('click', () => { offset += limit; refresh(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
refresh();
