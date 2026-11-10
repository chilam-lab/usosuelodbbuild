var verb_utils = {};

const initOptions = {
  error(error, e) {
    if (e.cn) {
      console.log('CN:', e.cn);
      console.log('EVENT:', error.message || error);
    }
  }
};

var pgp = require('pg-promise')(initOptions);
var config = require('../../config');

verb_utils.pool = pgp(config.db);
verb_utils.pool_mallas = pgp(config.db_mallas);

verb_utils.getParam = function (req, name, defaultValue) {
  var body = req.body || {};
  var query = req.query || {};

  if (body[name] != null) return body[name];
  if (query[name] != null) return query[name];

  return defaultValue;
};

module.exports = verb_utils;
