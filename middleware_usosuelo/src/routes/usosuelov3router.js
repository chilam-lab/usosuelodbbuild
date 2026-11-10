var debug = require('debug')('verbs:router');
var router = require('express').Router();
var usvCtrl = require('../controllers/usosuelov3_controller');
var verbUtils = require('../controllers/verb_utils.js');

router.all('/', function(req, res) {
  res.json({
    data: {
      message: '¡Yey! Bienvenido al API de Uso del Suelo y Vegetación de SPECIES v3'
    }
  });
});

router.all('/db-health', async (req, res) => {
  try {
    var db = verbUtils.pool;
    await db.one('SELECT 1 AS status');

    res.status(200).json({
      status: 'UP',
      message: 'database connected',
      timestamp: new Date().toISOString()
    });
  } catch (error) {
    debug(error);
    res.status(503).json({
      status: 'DOWN',
      message: 'database unreachable',
      error: error.message
    });
  }
});

router.route('/variables')
  .get(usvCtrl.variables)
  .post(usvCtrl.variables);

router.route('/secuencia')
  .get(usvCtrl.secuencia)
  .post(usvCtrl.secuencia);

router.route('/variables/:id')
  .get(usvCtrl.get_variable_byid)
  .post(usvCtrl.get_variable_byid);

router.route('/get-data/:id')
  .get(usvCtrl.get_data_byid)
  .post(usvCtrl.get_data_byid);

router.route('/info')
  .get(usvCtrl.get_sourceinfo)
  .post(usvCtrl.get_sourceinfo);

module.exports = router;
