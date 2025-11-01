
// index.js
import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import cookieParser from 'cookie-parser';
import crypto from 'crypto';
import { Client, GatewayIntentBits } from 'discord.js';
import { fileURLToPath } from 'url';
import path from 'path';

// ---- Rutas/paths ----
const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

// ---- ENV ----
const APP_ORIGIN   = process.env.APP_ORIGIN   || 'https://pjsk-page.vercel.app';
const COOKIE_NAME  = process.env.COOKIE_NAME  || 'sid';
const PORT         = parseInt(process.env.PORT, 10) || 3000;

const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const GUILD_ID      = process.env.GUILD_ID;

// ==== EXPRESS base (un solo servidor) ====
const app = express();
app.set('trust proxy', 1);
app.use(express.json());
app.use(cookieParser());

// CORS cross-site para Vercel con cookies
app.use(cors({
  origin: [APP_ORIGIN],
  credentials: true
}));
app.options('*', cors({
  origin: [APP_ORIGIN],
  credentials: true
}));

// Sirve estáticos si los usas (opcional)
app.use(express.static(path.join(__dirname, 'public')));

// ====== Sesión simple (usa DB/Redis en prod) ======
const sessionStore = new Map(); // sid -> { discordId, username }
const pfpMap       = new Map(); // key(nameLower|userId|discordId) -> url

function cryptoRandom(){ return crypto.randomBytes(24).toString('hex'); }

function setSessionCookie(res, sid){
  res.cookie(COOKIE_NAME, sid, {
    httpOnly: true,
    secure: true,
    sameSite: 'none',
    path: '/',
    maxAge: 1000 * 60 * 60 * 24 * 7
  });
}
function getSession(req){
  const sid = req.cookies?.[COOKIE_NAME];
  if(!sid) return null;
  return sessionStore.get(sid) || null;
}

// ========= RUTAS auth02 =========

// Simula callback OAuth (reemplaza por tu flujo de Discord real)
app.get('/auth02/callback', (req, res) => {
  // Aquí deberías intercambiar "code" por el token de Discord y leer user.
  // Mock mínimo:
  const discordUser = { id: '1234567890', username: 'Diego#0001' };

  const sid = cryptoRandom();
  sessionStore.set(sid, { discordId: discordUser.id, username: discordUser.username });
  setSessionCookie(res, sid);

  return res.redirect(302, `${APP_ORIGIN}/?verified=1`);
});

// Devuelve user si hay cookie de sesión
app.get('/auth02/me', (req, res) => {
  const sess = getSession(req);
  if (!sess) return res.status(401).json({ ok:false });
  return res.json({ id: sess.discordId, username: sess.username });
});

// Devuelve mapa de PFPs (para el leaderboard)
app.get('/auth02/pfps', (req, res) => {
  const obj = {};
  for (const [k,v] of pfpMap.entries()) obj[k] = v;
  res.json(obj);
});

// Guarda/borra PFP (requiere sesión)
app.post('/auth02/set-pfp', (req, res) => {
  const sess = getSession(req);
  if (!sess) return res.status(401).json({ ok:false, error:'unauthorized' });

  const { url, nameLower, userId } = req.body || {};
  const key = (nameLower && String(nameLower).toLowerCase())
          || (userId && String(userId))
          || sess.discordId;

  if (typeof url !== 'string') {
    return res.status(400).json({ ok:false, error:'invalid url' });
  }

  if (url === '' || url === null) {
    pfpMap.delete(key);
  } else {
    pfpMap.set(key.toLowerCase?.() ?? key, url);
  }
  return res.json({ ok:true });
});

// Mock de cartas (reemplaza por tu fuente real)
app.get('/auth02/cards', (req, res) => {
  const cards = [
    { id: 1, name: 'Miku 01',    thumb:'https://via.placeholder.com/512?text=Miku01' },
    { id: 2, name: 'Mafuyu 02',  thumb:'https://via.placeholder.com/512?text=Mafuyu02' },
    { id: 3, name: 'Ichika 03',  thumb:'https://via.placeholder.com/512?text=Ichika03' },
  ];
  res.json(cards);
});

// ===== (Opcional) APIs que ya tenías (/api/players, /api/seasons, etc.) =====
// Si esas viven en otro archivo, impórtalas y haz app.use(router).
// app.get('/api/players', ...);
// app.get('/api/seasons', ...);

// ========= DISCORD BOT =========
const wanted = ['Placement','Bronze','Gold','Diamond'];
const client = new Client({ intents: [ GatewayIntentBits.Guilds ] });

let rankColors = {};
async function loadRankColors() {
  const guild = await client.guilds.fetch(GUILD_ID);
  const roles = await guild.roles.fetch();
  const map = {};
  for (const role of roles.values()) {
    if (wanted.includes(role.name)) {
      map[role.name] = '#' + role.color.toString(16).padStart(6,'0');
    }
  }
  rankColors = map;
  console.log('✅ Colores cargados:', rankColors);
}

// APIs para colores y asignación de rol (si las usas en tu página)
app.get('/api/rank-colors', (req, res) => {
  res.json(rankColors);
});

app.post('/api/assign-rank', async (req, res) => {
  const { userId, newRank } = req.body || {};
  if (!userId || !rankColors[newRank]) {
    return res.status(400).json({ error: 'userId o newRank inválido' });
  }
  try {
    const guild  = await client.guilds.fetch(GUILD_ID);
    const member = await guild.members.fetch(userId);
    const allRoles = await guild.roles.fetch();
    // Quita roles previos
    const toRemove = member.roles.cache.filter(r => wanted.includes(r.name));
    await member.roles.remove(toRemove);
    // Agrega el nuevo
    const roleToAdd = allRoles.find(r => r.name === newRank);
    await member.roles.add(roleToAdd);
    res.json({ success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Error interno' });
  }
});

// Arranque: primero el bot, luego el server (o al revés; Express puede iniciar ya)
client.once('ready', async () => {
  console.log(`🤖 Bot listo: ${client.user.tag}`);
  await loadRankColors();
  client.on('roleCreate', r => wanted.includes(r.name) && loadRankColors());
  client.on('roleUpdate', (_, r) => wanted.includes(r.name) && loadRankColors());
});

// Inicia ambos
app.listen(PORT, () => {
  console.log(`🌐 Server listening on :${PORT}`);
});
client.login(DISCORD_TOKEN);
