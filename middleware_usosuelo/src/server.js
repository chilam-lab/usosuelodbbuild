var express = require('express');
var cors = require('cors');
var bodyParser = require('body-parser');
var config = require('../config');
var zlib = require('zlib');
var compression = require('compression');
var session = require('express-session');
var pgSession = require('connect-pg-simple')(session);
process.env.TZ = 'America/Mexico_City';
var verb_utils = require('./controllers/verb_utils');
var db = verb_utils.pool;

var port = config.port || 8080;
var app = express();

app.use(compression({ filter: shouldCompress, level: zlib.Z_BEST_COMPRESSION }));
function shouldCompress(req, res) {
  return compression.filter(req, res);
}

app.use(cors());
app.use(bodyParser.json({ limit: '512mb', extended: true }));
app.use(bodyParser.urlencoded({ limit: '512mb', extended: true, parameterLimit: 1000000 }));

app.use(session({
  store: new pgSession({
    pgPromise: db,
    tableName: 'session'
  }),
  secret: 'species_usv_key',
  cookie: { maxAge: 1 * 60 * 60 * 1000 },
  saveUninitialized: false,
  resave: false
}));

var usvv3Router = require('./routes/usosuelov3router');
app.use('/usosuelo', usvv3Router);

var server = app.listen(port, function () {
  var listenPort = server.address().port;
  console.log('Aplicación USV corriendo en el puerto %s', listenPort);
});

server.setTimeout(60 * 1000 * 15);
module.exports = server;
