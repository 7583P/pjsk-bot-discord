// index.js
import 'dotenv/config';
import express from 'express';
import { Client, GatewayIntentBits } from 'discord.js';
import { fileURLToPath } from 'url';
import path from 'path';

// ---- Configuración de rutas para ES Modules ----
const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

// ---- Variables de entorno ----
const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const GUILD_ID      = process.env.GUILD_ID;
const PORT          = parseInt(process.env.PORT, 10) || 3001;

// ---- Roles que quieres exponer ----
const wanted = ['Placement','Bronze','Gold','Diamond'];

// ---- Inicializa Discord.js ----
const client = new Client({ intents: [ GatewayIntentBits.Guilds ] });

// ---- Mapeo rol→color ----
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

// ---- Cuando el bot esté listo ----
client.once('ready', async () => {
  console.log(`🤖 Bot listo: ${client.user.tag}`);
  await loadRankColors();

  // Recarga si se crean o actualizan roles relevantes
  client.on('roleCreate', r => wanted.includes(r.name) && loadRankColors());
  client.on('roleUpdate', (_, r) => wanted.includes(r.name) && loadRankColors());

  // ---- Express setup ----
  const app = express();
  app.use(express.json());

  // 1) Sirve estáticos desde public/
  app.use(express.static(path.join(__dirname, 'public')));

  // 2) API: colores de rol
  app.get('/api/rank-colors', (req, res) => {
    res.json(rankColors);
  });

  // 3) API: asignar rol al usuario
  app.post('/api/assign-rank', async (req, res) => {
    const { userId, newRank } = req.body;
    if (!userId || !rankColors[newRank]) {
      return res.status(400).json({ error: 'userId o newRank inválido' });
    }
    try {
      const guild  = await client.guilds.fetch(GUILD_ID);
      const member = await guild.members.fetch(userId);
      const allRoles = await guild.roles.fetch();
      // Quita roles anteriores
      const toRemove = member.roles.cache.filter(r => wanted.includes(r.name));
      await member.roles.remove(toRemove);
      // Añade el rol nuevo
      const roleToAdd = allRoles.find(r => r.name === newRank);
      await member.roles.add(roleToAdd);
      res.json({ success: true });
    } catch (err) {
      console.error(err);
      res.status(500).json({ error: 'Error interno' });
    }
  });

  // 4) Arranca el servidor
  app.listen(PORT, () => {
    console.log(`🌐 Web y API escuchando en http://localhost:${PORT}`);
  });
});

// ---- Inicia sesión en Discord ----
client.login(DISCORD_TOKEN);

