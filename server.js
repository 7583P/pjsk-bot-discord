// server.js
import express from 'express';
import cors from 'cors';
import cookieParser from 'cookie-parser';
import crypto from 'crypto';

const APP_ORIGIN    = process.env.APP_ORIGIN    || 'https://pjsk-page.vercel.app';
const PORT          = process.env.PORT          || 3000;
const COOKIE_NAME   = process.env.COOKIE_NAME   || 'sid';

const app = express();
app.set('trust proxy', 1);
app.use(express.json());
app.use(cookieParser());

app.use(cors({
  origin: [APP_ORIGIN],
  credentials: true
}));
app.options('*', cors({
  origin: [APP_ORIGIN],
  credentials: true
}));

// ===== Mock stores (usa DB/Redis en prod) =====
const sessionStore = new Map(); // sid -> { discordId, username }
const pfpMap       = new Map(); // key(nameLower|userId|discordId) -> url

// ===== Helpers =====
function cryptoRandom() {
  return crypto.randomBytes(24).toString('hex');
}
function setSessionCookie(res, sid) {
  res.cookie(COOKIE_NAME, sid, {
    httpOnly: true,
    secure: true,
    sameSite: 'none',
    path: '/',
    maxAge: 1000 * 60 * 60 * 24 * 7
  });
}
function getSession(req) {
  const sid = req.cookies?.[COOKIE_NAME];
  if (!sid) return null;
  return sessionStore.get(sid) || null;
}

// ===== Rutas auth02 =====

// Simula callback OAuth (reemplaza por tu flujo Discord real)
app.get('/auth02/callback', (req, res) => {
  const discordUser = { id: '1234567890', username: 'Diego#0001' };
  const sid = cryptoRandom();
  sessionStore.set(sid, { discordId: discordUser.id, username: discordUser.username });
  setSessionCookie(res, sid);
  return res.redirect(302, `${APP_ORIGIN}/?verified=1`);
});

// Devuelve user desde cookie de sesión
app.get('/auth02/me', (req, res) => {
  const sess = getSession(req);
  if (!sess) return res.status(401).json({ ok:false });
  return res.json({ id: sess.discordId, username: sess.username });
});

// Devuelve mapa de PFPs (para el leaderboard)
app.get('/auth02/pfps', (req, res) => {
  const obj = {};
  for (const [k, v] of pfpMap.entries()) obj[k] = v;
  res.json(obj);
});

// Guarda/borra PFP; requiere sesión válida
app.post('/auth02/set-pfp', (req, res) => {
  const sess = getSession(req);
  if (!sess) return res.status(401).json({ ok:false, error:'unauthorized' });

  const { url, nameLower, userId } = req.body || {};
  // prioridad: nameLower > userId > discordId de la sesión
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

// Mock simple de cartas (reemplaza por tu fuente real)
app.get('/auth02/cards', (req, res) => {
  const cards = [
    { id: 1, name: 'Miku 01', thumb: 'https://via.placeholder.com/512?text=Miku01' },
    { id: 2, name: 'Mafuyu 02', thumb: 'https://via.placeholder.com/512?text=Mafuyu02' },
    { id: 3, name: 'Ichika 03', thumb: 'https://via.placeholder.com/512?text=Ichika03' },
  ];
  res.json(cards);
});

app.listen(PORT, () => {
  console.log(`auth02 server listening on :${PORT}`);
});
