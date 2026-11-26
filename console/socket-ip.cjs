// Preloaded before Next's server.js (Dockerfile CMD, `npm start`). Next gives route code no access to the connection and keeps an
// X-Forwarded-For the client sent, so no header from the client can say where a browser really is. This stamps every request with the
// socket's own address, over whatever the client put there; lib/auth.ts fromLan() reads it for "inside the LAN = no password".
const http = require("node:http");
const createServer = http.createServer;
http.createServer = (...args) =>
  createServer(...args).prependListener("request", (req) => { req.headers["x-bt-socket-ip"] = req.socket.remoteAddress ?? ""; });
