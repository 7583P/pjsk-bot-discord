// server.js
const express = require('express');
const http    = require('http');
const cors    = require('cors');
const { Server } = require('socket.io');

const app    = express();
const server = http.createServer(app);
const io     = new Server(server, {
  cors: { origin: '*' }  // o pon tu dominio: 'https://mi-web.com'
});

app.use(express.json());
app.use(cors());

// Endpoint que llamará el bot tras !submit5
app.post('/api/notify', (req, res) => {
  const player = req.body;
  io.emit('mmrUpdated', player);
  res.sendStatus(200);
});

io.on('connection', socket => {
  console.log('👉 Cliente conectado:', socket.id);
});

server.listen(3000, () => {
  console.log('🔊 Socket.IO escuchando en puerto 3000');
});
