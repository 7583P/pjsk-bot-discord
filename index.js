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

// ==== EXPRESS base ====
const app = express();
app.set('trust proxy', 1);
app.use(express.json());
app.use(cookieParser());

// --- CORS middlewares ---
// Público (no cookies, libre lectura desde cualquier origen)
const corsPublic = cors({
  origin: '*',
  credentials: false,
  methods: ['GET', 'HEAD', 'OPTIONS'],
  allowedHeaders: ['Content-Type'],
});

// Protegido (con cookies cross-site; SOLO tu frontend)
const corsCreds = cors({
  origin: APP_ORIGIN,   // tu frontend (no uses '*')
  credentials: true,
  methods: ['GET', 'POST', 'OPTIONS'],
  allowedHeaders: ['Content-Type'],
});

// Sirve estáticos si los usas (opcional)
app.use(express.static(path.join(__dirname, 'public')));

// ====== Sesión simple (usa DB/Redis en prod) ======
const sessionStore = new Map();   // sid -> { discordId, username }
const pfpById      = new Map();   // "discordId" -> url
const pfpByName    = new Map();   // "nameLower" -> url

function cryptoRandom(){ return crypto.randomBytes(24).toString('hex'); }

function setSessionCookie(res, sid){
  res.cookie(COOKIE_NAME, sid, {
    httpOnly: true,
    secure: true,        // obligatorio con SameSite=None en HTTPS
    sameSite: 'none',
    path: '/',
    maxAge: 1000 * 60 * 60 * 24 * 7, // 7 días
  });
}
function getSession(req){
  const sid = req.cookies?.[COOKIE_NAME];
  if(!sid) return null;
  return sessionStore.get(sid) || null;
}

// ========= RUTAS auth02 =========

// Iniciar el flujo (mock). En prod, aquí rediriges a Discord OAuth.
app.get('/auth02', corsCreds, (req, res) => {
  // Para el mock redirigimos directo al callback para crear sesión
  return res.redirect(302, '/auth02/callback');
});

// Simula callback OAuth (reemplaza por tu flujo real con Discord)
app.get('/auth02/callback', corsCreds, (req, res) => {
  // Aquí deberías intercambiar "code" por token y leer user de Discord.
  // Mock mínimo:
  const discordUser = { id: '1234567890', username: 'Diego#0001' };

  const sid = cryptoRandom();
  sessionStore.set(sid, { discordId: discordUser.id, username: discordUser.username });
  setSessionCookie(res, sid);

  return res.redirect(302, `${APP_ORIGIN}/?verified=1`);
});

// Devuelve user si hay cookie de sesión (protegido)
app.get('/auth02/me', corsCreds, (req, res) => {
  const sess = getSession(req);
  if (!sess) return res.status(401).json({ ok:false });
  return res.json({ id: sess.discordId, username: sess.username });
});

// Devuelve mapa de PFPs (PÚBLICO, para tu front sin credenciales)
// Estructura esperada por tu Vue: { byId: {...}, byName: {...} }
app.get('/auth02/pfps', corsPublic, (req, res) => {
  const byId = {};
  for (const [k, v] of pfpById.entries()) byId[k] = v;

  const byName = {};
  for (const [k, v] of pfpByName.entries()) byName[k] = v;

  res.json({ byId, byName });
});

// Guarda/borra PFP (PROTEGIDO)
app.post('/auth02/set-pfp', corsCreds, (req, res) => {
  const sess = getSession(req);
  if (!sess) return res.status(401).json({ ok:false, error:'unauthorized' });

  const { url, nameLower, userId } = req.body || {};
  if (typeof url !== 'string') {
    return res.status(400).json({ ok:false, error:'invalid url' });
  }

  // Normaliza llaves
  const nameKey = (nameLower ? String(nameLower).toLowerCase() : null);
  // si no envían userId, usamos el discordId de la sesión
  const idKey   = (userId ? String(userId) : String(sess.discordId));

  if (url === '') {
    // Borrar
    if (idKey)   pfpById.delete(idKey);
    if (nameKey) pfpByName.delete(nameKey);
  } else {
    // Guardar en ambos mapas si hay data
    if (idKey)   pfpById.set(idKey, url);
    if (nameKey) pfpByName.set(nameKey, url);
  }

  return res.json({ ok:true });
});

// Mock de cartas (PÚBLICO). Reemplaza por tu fuente real (Sekaipedia, etc.)
app.get('/auth02/cards', corsPublic, (req, res) => {
  const cards = [
    { id: 1, name: 'Miku 01',   thumb:'https://via.placeholder.com/512?text=Miku01' },
    { id: 2, name: 'Mafuyu 02', thumb:'https://via.placeholder.com/512?text=Mafuyu02' },
    { id: 3, name: 'Ichika 03', thumb:'https://via.placeholder.com/512?text=Ichika03' },
  ];
  res.json(cards);
});

// Preflight (OPTIONS) explícito para ambas políticas
app.options(['/auth02','/auth02/me','/auth02/set-pfp','/auth02/callback'], corsCreds);
app.options(['/auth02/cards','/auth02/pfps'], corsPublic);

// ===== (Opcional) APIs que ya tenías (/api/players, /api/seasons, etc.) =====
// Ejemplo de capa CORS protegida si las usas con cookies:
app.options(['/api/players','/api/seasons'], corsCreds);
// app.get('/api/players', corsCreds, ...);
// app.get('/api/seasons', corsCreds, ...);

// ========= DISCORD BOT =========
const wanted = ['Placement','Bronze','Gold','Diamond'];
const client = new Client({ intents: [ GatewayIntentBits.Guilds ] });

let rankColors = {};
async function loadRankColors() {
  if (!GUILD_ID) return;
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
app.get('/api/rank-colors', corsCreds, (req, res) => {
  res.json(rankColors);
});

app.post('/api/assign-rank', corsCreds, async (req, res) => {
  const { userId, newRank } = req.body || {};
  if (!userId || !rankColors[newRank]) {
    return res.status(400).json({ error: 'userId o newRank inválido' });
  }
  try {
    if (!GUILD_ID) throw new Error('Falta GUILD_ID');
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

// Healthcheck (útil para Railway)
app.get('/health', (req, res) => res.json({ ok: true }));

// Arranque bot (si hay token) y server
if (DISCORD_TOKEN) {
  client.once('ready', async () => {
    console.log(`🤖 Bot listo: ${client.user.tag}`);
    await loadRankColors();
    client.on('roleCreate', r => wanted.includes(r.name) && loadRankColors());
    client.on('roleUpdate', (_, r) => wanted.includes(r.name) && loadRankColors());
  });
  client.login(DISCORD_TOKEN).catch(err => {
    console.error('Error al iniciar el bot de Discord:', err.message);
  });
} else {
  console.warn('⚠️ DISCORD_TOKEN no definido. El bot no se iniciará.');
}

app.listen(PORT, () => {
  console.log(`🌐 Server listening on :${PORT}`);
});
