require('dotenv').config();

const config = {
  db: {
    database: process.env.DBNAME,
    user: process.env.DBUSER,
    password: process.env.DBPWD,
    host: process.env.DBHOST,
    port: process.env.DBPORT,
    application_name: 'SPECIESV3_USV_Middleware',
    ssl: false,
    poolSize: 10,
    connectionTimeoutMillis: Number(process.env.DB_CONNECTION_TIMEOUT_MS || 5000),
    idleTimeoutMillis: Number(process.env.DB_IDLE_TIMEOUT_MS || 30000),
    query_timeout: Number(process.env.DB_QUERY_TIMEOUT_MS || 60000),
    statement_timeout: Number(process.env.DB_STATEMENT_TIMEOUT_MS || 60000),
    keepAlive: true,
  },
  db_mallas: {
    database: process.env.DBNAME_MALLAS,
    user: process.env.DBUSER_MALLAS,
    password: process.env.DBPWD_MALLAS,
    host: process.env.DBHOST_MALLAS,
    port: process.env.DBPORT_MALLAS,
    application_name: 'MallasV3_USV_Middleware',
    ssl: false,
    poolSize: 10,
    // work_mem por conexión de este pool (no global): todo lo que sale de
    // db_mallas es cruce espacial, así que subirlo aquí no requiere clasificar
    // cada query como "pesada" caso por caso.
    options: '-c work_mem=32MB',
    connectionTimeoutMillis: Number(process.env.DB_CONNECTION_TIMEOUT_MS || 5000),
    idleTimeoutMillis: Number(process.env.DB_IDLE_TIMEOUT_MS || 30000),
    query_timeout: Number(process.env.DB_QUERY_TIMEOUT_MS || 60000),
    statement_timeout: Number(process.env.DB_STATEMENT_TIMEOUT_MS || 60000),
    keepAlive: true,
  },
  port: process.env.PORT || 8080,
};

module.exports = config;
