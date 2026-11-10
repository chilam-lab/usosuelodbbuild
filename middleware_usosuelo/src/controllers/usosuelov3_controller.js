var debug = require('debug')('verbs:controllers');
var verb_utils = require('./verb_utils');
var pgp = require('pg-promise')();

var pool = verb_utils.pool;
var pool_mallas = verb_utils.pool_mallas;

var MAX_LIMIT = 500;
var MAX_LEVELS = 500;
var GEOM_CHUNK_SIZE = 500;

// Nombre del nivel de clases (el nivel selector es usv_source_vars.variable_name,
// ej. 'usv_serie_vii'). No se usa 'categoria' porque el front ya lo traduce
// como "Rango de elevación" para DEM.
var CATEGORY_LEVEL = 'clase_usv';

const gridResolutionCache = new Map(); // grid_id → { cellColumn, resolvedTable }
const usvVarCache = new Map();          // variable_id → { series, usvTableName }
const resultCache = new Map();          // cacheKey → { data, ts }
const RESULT_CACHE_TTL_MS = 60 * 60 * 1000; // 1 hora

function buildResultCacheKey(variable_id, grid_id, levels_id, filter_names, filter_values) {
  const lvls = levels_id.slice().sort((a, b) => a - b).join(',');
  const filters = filter_names.map((n, i) => `${n}=${filter_values[i]}`).join(';');
  return `${variable_id}:${grid_id}:${lvls}:${filters}`;
}

async function resolveGridTableName(poolMallas, tableCellName) {
  const candidate = String(tableCellName || '').trim().toLowerCase();
  if (!candidate) return null;

  const reg = await poolMallas.oneOrNone('SELECT to_regclass($1) AS reg', [`public.${candidate}`]);
  if (reg && reg.reg) return candidate;

  const fallbackMap = {
    grid_64km_aoi: 'grid_geojson_64km_aoi',
    grid_32km_aoi: 'grid_geojson_32km_aoi',
    grid_16km_aoi: 'grid_geojson_16km_aoi',
    grid_8km_aoi: 'grid_geojson_8km_aoi',
    grid_state_aoi: 'grid_geojson_state_aoi',
    grid_mun_aoi: 'grid_geojson_mun_aoi',
    grid_ageb_aoi: 'grid_geojson_ageb_aoi',
    grid_cue_aoi: 'grid_geojson_cue_aoi',
  };

  const fallback = fallbackMap[candidate];
  if (!fallback) return null;

  const regFallback = await poolMallas.oneOrNone('SELECT to_regclass($1) AS reg', [`public.${fallback}`]);
  if (regFallback && regFallback.reg) return fallback;

  // fallback adicional por nombres inconsistentes detectados en algunos catálogos
  const normalizedRes = candidate
    .replace(/^grid_/, '')
    .replace(/^gri_d/, '')
    .replace(/_aoi$/, '');
  const extraCandidates = [
    `grid_${normalizedRes}_aoi`,
    `grid_geojson_${normalizedRes}_aoi`,
    `gri_d${normalizedRes}_aoi`,
  ];

  for (const tbl of extraCandidates) {
    const regExtra = await poolMallas.oneOrNone('SELECT to_regclass($1) AS reg', [`public.${tbl}`]);
    if (regExtra && regExtra.reg) return tbl;
  }

  return null;
}

const ALLOWED_CELL_TABLES = [
  'grid_64km_aoi',
  'grid_32km_aoi',
  'grid_16km_aoi',
  'grid_8km_aoi',
  'grid_state_aoi',
  'grid_mun_aoi',
  'grid_ageb_aoi',
  'grid_cue_aoi'
];

async function getUsvVar(variable_id) {
  if (usvVarCache.has(variable_id)) return usvVarCache.get(variable_id);

  const variableRow = await pool.oneOrNone(
    'SELECT id, series FROM usv_source_vars WHERE id = $1',
    [variable_id]
  );
  if (!variableRow) return null;

  const usvTableName = `usv_${variableRow.series}`;
  const tableReg = await pool.oneOrNone('SELECT to_regclass($1) AS reg', [`public.${usvTableName}`]);
  if (!tableReg || !tableReg.reg) return null;

  const result = { series: variableRow.series, usvTableName };
  usvVarCache.set(variable_id, result);
  return result;
}

async function getGridResolution(grid_id) {
  if (gridResolutionCache.has(grid_id)) return gridResolutionCache.get(grid_id);

  const gridInfo = await pool_mallas.oneOrNone(
    'SELECT resolution, table_cell_name, table_view_name, region_id FROM cat_grid WHERE grid_id = $1',
    [grid_id]
  );
  if (!gridInfo) return null;

  const tableCellName = String(gridInfo.table_cell_name || '').trim();
  const cellColumn = `gridid_${String(gridInfo.resolution).toLowerCase()}`;

  if (!ALLOWED_CELL_TABLES.includes(tableCellName)) return null;

  const resolvedTable = await resolveGridTableName(pool_mallas, tableCellName);
  if (!resolvedTable) return null;

  const colExists = await pool_mallas.oneOrNone(
    `SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2`,
    [resolvedTable, cellColumn]
  );
  if (!colExists) return null;

  const tableViewName = String(gridInfo.table_view_name || '').trim();
  const region_id     = gridInfo.region_id || null;

  const result = { cellColumn, resolvedTable, tableViewName, region_id };
  gridResolutionCache.set(grid_id, result);
  return result;
}

// Metadatos de una clase, iguales en /variables/:id y en get-data.
const CATEGORY_METADATA_SQL = `jsonb_build_object(
          'clase_usv', c.clave,
          'clave', c.clave,
          'label', c.label,
          'grupo', c.grupo,
          'layer', c.layer,
          'cat_index', c.cat_index,
          'area_km2', round(c.area_km2::numeric, 2)
        )`;

exports.variables = async function (req, res) {
  try {
    const sourceVars = await pool.any(
      `SELECT
         id,
         lower(variable_name) AS variable,
         level_size,
         filter_fields,
         available_grids
       FROM usv_source_vars
       ORDER BY id;`,
      {}
    );

    // Expone dos niveles por variable, como DEM: el nivel selector (la serie)
    // y el nivel de clases de uso de suelo.
    const data = [];
    for (const sv of sourceVars) {
      data.push({
        id: sv.id * 2 - 1,           // 1 para la primera serie
        variable: sv.variable,         // 'usv_serie_vii'  ← nivel del selector
        level_size: 1,
        filter_fields: {},
        available_grids: sv.available_grids
      });
      data.push({
        id: sv.id * 2,                // 2 para la primera serie
        variable: CATEGORY_LEVEL,      // ← nivel del navegador (clases)
        level_size: sv.level_size,
        filter_fields: { clase_usv: 'string', grupo: 'string' },
        available_grids: sv.available_grids
      });
    }

    return res.status(200).json({ data });
  } catch (error) {
    debug(error);
    return res.status(500).json({
      message: 'Error interno al obtener el catálogo de variables de uso de suelo'
    });
  }
};

exports.get_variable_byid = async function (req, res) {
  try {
    const virtual_id = Number(req.params.id);
    const q = verb_utils.getParam(req, 'q', '');
    const offset = Number(verb_utils.getParam(req, 'offset', 0));
    const limit = Math.min(Number(verb_utils.getParam(req, 'limit', 10)), MAX_LIMIT);

    if (!Number.isInteger(virtual_id) || virtual_id <= 0) {
      return res.status(400).json({ message: 'El parámetro id es inválido' });
    }

    if (!Number.isInteger(offset) || offset < 0 || !Number.isInteger(limit) || limit <= 0) {
      return res.status(400).json({ message: 'Parámetros offset/limit inválidos' });
    }

    // Las IDs virtuales del endpoint /variables son id*2-1 (nivel padre) y id*2 (nivel hijo).
    // Mapear de vuelta al source_var_id real en usv_source_vars.
    let variableRow = await pool.oneOrNone(
      'SELECT id, lower(variable_name) AS varname FROM usv_source_vars WHERE id = $1',
      [virtual_id]
    );
    let source_var_id = virtual_id;

    if (!variableRow) {
      const real_id = Math.ceil(virtual_id / 2);
      variableRow = await pool.oneOrNone(
        'SELECT id, lower(variable_name) AS varname FROM usv_source_vars WHERE id = $1',
        [real_id]
      );
      if (!variableRow) {
        return res.status(404).json({ message: 'No existe la variable solicitada' });
      }
      source_var_id = real_id;
    }

    const varName = variableRow.varname ?? '';

    // ─── Modo A: búsqueda del taxon-selector ─────────────────────────────────────
    // mdf envía: q = "usv_serie_vii = <texto>" — el nombre de la variable como filtro.
    // Devuelve la serie como item seleccionable (no las clases).
    if (q !== '') {
      const firstSegment = q.split(';')[0].trim();
      const eqIdx = firstSegment.indexOf('=');
      if (eqIdx > -1) {
        const filterKey = firstSegment.slice(0, eqIdx).trim().toLowerCase();
        if (filterKey === varName) {
          // Hay muy pocas series (1-3): se devuelven todas sin filtrar por texto.
          const sourceRows = await pool.any(
            `SELECT id, variable_name, descripcion, series FROM usv_source_vars ORDER BY id LIMIT $1 OFFSET $2`,
            [limit, offset]
          );
          const data = sourceRows.map(row => ({
            id: virtual_id,
            level_id: [row.id],
            data: {
              idfuente: row.id,
              descripcion: row.descripcion,
              series: row.series
            }
          }));
          return res.status(200).json({ data });
        }
      }
    }

    // ─── Modo B/C: retorna clases ─────────────────────────────────────────────────
    // Usado por el análisis (q = "idfuente=X; clase_usv=BQ,TA") y por getTaxonChildren.
    const conditions = ['c.source_var_id = $1'];
    const values = [source_var_id];
    let paramIdx = 2;

    if (q !== '') {
      const array_queries = q.split(';').map(x => x.trim()).filter(Boolean);

      for (const filter of array_queries) {
        const eqIdx = filter.indexOf('=');
        if (eqIdx === -1) continue;

        const filter_param = filter.slice(0, eqIdx).trim().toLowerCase();
        const filter_values = filter.slice(eqIdx + 1).trim().split(',').map(v => v.trim()).filter(Boolean);
        if (filter_values.length === 0) continue;

        if (filter_param === 'levels_id') {
          const ids = filter_values.map(Number).filter(n => Number.isInteger(n) && n > 0);
          if (ids.length > 0) {
            conditions.push(`c.id IN ($${paramIdx}:csv)`);
            values.push(ids);
            paramIdx += 1;
          }
          continue;
        }

        if (filter_param === CATEGORY_LEVEL || filter_param === 'categoria') {
          // Una clase se puede pedir por id interno (selección desde el árbol),
          // por su CLAVE de INEGI (ej. "BQ", "VSa/SBC") o por texto de su
          // descripción (ej. "encino"); se prueban todas a la vez (OR).
          const orParts = [];

          for (const raw of filter_values) {
            const trimmed = String(raw).trim();
            if (!trimmed) continue;

            if (/^\d+$/.test(trimmed)) {
              orParts.push(`c.id = $${paramIdx}`);
              values.push(Number(trimmed));
              paramIdx += 1;
              continue;
            }

            orParts.push(`lower(c.clave) = lower($${paramIdx})`);
            values.push(trimmed);
            paramIdx += 1;

            orParts.push(`unaccent_label(c.label) ILIKE unaccent_label($${paramIdx})`);
            values.push(`%${trimmed}%`);
            paramIdx += 1;
          }

          if (orParts.length === 0) {
            return res.status(200).json({ data: [] });
          }
          conditions.push(`(${orParts.join(' OR ')})`);
          continue;
        }

        if (filter_param === 'grupo') {
          conditions.push(`lower(c.grupo) IN ($${paramIdx}:csv)`);
          values.push(filter_values.map(v => v.toLowerCase()));
          paramIdx += 1;
          continue;
        }

        if (filter_param === 'layer') {
          conditions.push(`lower(c.layer) IN ($${paramIdx}:csv)`);
          values.push(filter_values.map(v => v.toLowerCase()));
          paramIdx += 1;
          continue;
        }

        if (filter_param === 'idfuente') {
          // idfuente equivale a source_var_id
          const ids = filter_values.map(Number).filter(n => Number.isInteger(n) && n > 0);
          if (ids.length > 0) {
            conditions.push(`c.source_var_id IN ($${paramIdx}:csv)`);
            values.push(ids);
            paramIdx += 1;
          }
          continue;
        }

        if (filter_param === 'clave') {
          conditions.push(`c.clave IN ($${paramIdx}:csv)`);
          values.push(filter_values);
          paramIdx += 1;
        }
        // Filtros desconocidos se ignoran para no bloquear consultas del orquestador
      }
    }

    const whereSql = `WHERE ${conditions.join(' AND ')}`;

    const query = `
      SELECT
        s.id AS id,
        c.id AS level_id,
        jsonb_build_object('idfuente', s.id) || ${CATEGORY_METADATA_SQL} AS datos
      FROM usv_categories c
      JOIN usv_source_vars s ON s.id = c.source_var_id
      ${whereSql}
      ORDER BY c.cat_index
      OFFSET ${offset}
      LIMIT ${limit};
    `;

    const rows = await pool.any(query, values);
    // El frontend (taxon-selector) espera la clave "data" (igual que WorldClim/DEM).
    const data = rows.map(row => ({ id: row.id, level_id: row.level_id, data: row.datos }));
    return res.status(200).json({ data });
  } catch (error) {
    debug(error);
    return res.status(500).json({
      message: 'Error interno al obtener clases de uso de suelo'
    });
  }
};

exports.get_data_byid = async function (req, res) {
  try {
    const variable_id = Number(req.params.id);
    const grid_id = Number(verb_utils.getParam(req, 'grid_id', 1));
    const levels_id_raw = verb_utils.getParam(req, 'levels_id', []);
    const filter_names = verb_utils.getParam(req, 'filter_names', []);
    const filter_values = verb_utils.getParam(req, 'filter_values', []);

    if (!Number.isInteger(variable_id) || variable_id <= 0) {
      return res.status(400).json({ message: 'El parámetro id es inválido' });
    }

    if (!Number.isInteger(grid_id) || grid_id <= 0) {
      return res.status(400).json({ message: 'El parámetro grid_id es inválido' });
    }

    const levels_id_arr = Array.isArray(levels_id_raw) ? levels_id_raw : [levels_id_raw];
    const levels_id = levels_id_arr.map(Number).filter((n) => Number.isInteger(n) && n > 0);

    if (levels_id.length === 0) {
      return res.status(400).json({ message: 'levels_id debe ser un arreglo con al menos un valor válido' });
    }
    if (levels_id.length > MAX_LEVELS) {
      return res.status(400).json({ message: `levels_id excede el máximo permitido (${MAX_LEVELS})` });
    }

    if (!Array.isArray(filter_names) || !Array.isArray(filter_values) || filter_names.length !== filter_values.length) {
      return res.status(400).json({ message: 'filter_names y filter_values deben ser arreglos del mismo tamaño' });
    }
    if (!filter_names.every((f) => typeof f === 'string')) {
      return res.status(400).json({ message: 'filter_names debe contener solo strings' });
    }

    const cacheKey = buildResultCacheKey(variable_id, grid_id, levels_id, filter_names, filter_values);
    const cached = resultCache.get(cacheKey);
    if (cached && Date.now() - cached.ts < RESULT_CACHE_TTL_MS) {
      return res.status(200).json(cached.data);
    }

    // Resolución en paralelo — ambas funciones tienen caché interno
    const [usvVar, gridRes] = await Promise.all([
      getUsvVar(variable_id),
      getGridResolution(grid_id)
    ]);

    if (!usvVar) {
      return res.status(404).json({ message: 'No existe la variable solicitada o su tabla espacial' });
    }
    if (!gridRes) {
      return res.status(404).json({ message: 'No existe la malla solicitada o su configuración es inválida' });
    }

    const { usvTableName } = usvVar;
    const { cellColumn, resolvedTable: resolvedCellTable, tableViewName, region_id } = gridRes;

    const conditionParts = ['c.source_var_id = $1', 'c.id IN ($2:csv)'];
    const values = [variable_id, levels_id];
    let paramIdx = 3;

    for (let i = 0; i < filter_names.length; i++) {
      const filterParam = filter_names[i];
      const filterValue = filter_values[i];

      if (filterParam === CATEGORY_LEVEL || filterParam === 'clave') {
        conditionParts.push(`lower(c.clave) = lower($${paramIdx})`);
        values.push(String(filterValue));
        paramIdx += 1;
      } else if (filterParam === 'grupo') {
        conditionParts.push(`lower(c.grupo) = lower($${paramIdx})`);
        values.push(String(filterValue));
        paramIdx += 1;
      } else if (filterParam === 'layer') {
        conditionParts.push(`lower(c.layer) = lower($${paramIdx})`);
        values.push(String(filterValue));
        paramIdx += 1;
      } else {
        return res.status(400).json({ message: `Filtro no válido: ${filterParam}` });
      }
    }

    // Celdas precalculadas por clase y malla (dbbuild/precompute_usv_cells.py).
    // Solo sin filtros (los filtros cambian qué clases aplican); lo que no esté
    // en usv_cells se sigue calculando en vivo abajo.
    let cachedResponse = [];
    if (filter_names.length === 0) {
      const cachedRows = await pool.any(
        `SELECT u.cid, u.cells, c.cat_index, ${CATEGORY_METADATA_SQL} AS metadata
         FROM usv_cells u
         JOIN usv_categories c ON c.id = u.cid
         WHERE u.grid_id = $1 AND u.cid IN ($2:csv) AND c.source_var_id = $3`,
        [grid_id, levels_id, variable_id]
      ).catch((err) => { debug('usv_cells lookup:', err.message); return []; });

      cachedResponse = cachedRows.map((r) => ({
        id: variable_id, grid_id, level_id: Number(r.cid), metadata: r.metadata,
        cells: r.cells || [], n: (r.cells || []).length, cat_index: r.cat_index
      }));
    }

    const cachedCids = new Set(cachedResponse.map((r) => r.level_id));
    const pendingLevels = levels_id.filter((id) => !cachedCids.has(id));

    const finish = (liveArray) => {
      const merged = cachedResponse
        .concat(liveArray.map((r) => ({ ...r, level_id: Number(r.level_id), cat_index: r.metadata && r.metadata.cat_index })))
        .sort((a, b) => a.cat_index - b.cat_index)
        .map(({ cat_index, ...r }) => r);
      resultCache.set(cacheKey, { data: merged, ts: Date.now() });
      return res.status(200).json(merged);
    };

    if (pendingLevels.length === 0) {
      return finish([]);
    }
    values[1] = pendingLevels;

    const whereSql = `WHERE ${conditionParts.join(' AND ')}`;

    const query = `
      WITH numbered AS (
        SELECT
          p.the_geom,
          c.id AS cid,
          floor((row_number() OVER (PARTITION BY c.id ORDER BY p.id) - 1) / ${GEOM_CHUNK_SIZE}) AS chunk_idx
        FROM ${pgp.as.name(usvTableName)} p
        JOIN usv_categories c ON c.id = p.category_id
        ${whereSql}
      ),
      chunks AS (
        SELECT cid, chunk_idx, ST_AsText(ST_Collect(the_geom)) AS geom_chunk
        FROM numbered
        GROUP BY cid, chunk_idx
      )
      SELECT
        $1::int AS id,
        ch.cid AS level_id,
        ch.chunk_idx,
        ${CATEGORY_METADATA_SQL} AS metadata,
        ch.geom_chunk
      FROM chunks ch
      JOIN usv_categories c ON c.id = ch.cid
      ORDER BY c.cat_index, ch.chunk_idx;
    `;

    const data = await pool.any(query, values);
    if (!data || data.length === 0) {
      return finish([]);
    }

    // Recorta las celdas a la región del grid antes de intersectar con los
    // polígonos de la clase, evitando que nj supere el total n de la región.
    const meshQuery = (tableViewName && region_id)
      ? `
        WITH regionarea AS (
          SELECT g.${pgp.as.name(cellColumn)} AS cell, g.the_geom
          FROM ${pgp.as.name(resolvedCellTable)} g
          JOIN ${pgp.as.name(tableViewName)} vg ON ST_Intersects(g.the_geom, vg.border)
          WHERE vg.region_id = ${region_id}
        )
        SELECT DISTINCT ra.cell
        FROM (SELECT ST_Subdivide(ST_GeomFromText($1, 4326), 64) AS geom) sub
        JOIN regionarea ra ON ST_Intersects(ra.the_geom, sub.geom)
        ORDER BY cell;
      `
      : `
        SELECT DISTINCT g.${pgp.as.name(cellColumn)} AS cell
        FROM (SELECT ST_Subdivide(ST_GeomFromText($1, 4326), 64) AS geom) sub
        JOIN ${pgp.as.name(resolvedCellTable)} g ON ST_Intersects(g.the_geom, sub.geom)
        ORDER BY cell;
      `;

    // Agrupar chunks por level_id
    const levelMap = new Map();
    for (const row of data) {
      if (!levelMap.has(row.level_id)) {
        levelMap.set(row.level_id, { id: row.id, level_id: row.level_id, metadata: row.metadata, geomChunks: [] });
      }
      if (row.geom_chunk) {
        levelMap.get(row.level_id).geomChunks.push(row.geom_chunk);
      }
    }

    // Por cada clase: todos sus chunks en paralelo → deduplicar celdas
    const responseArray = await Promise.all(
      Array.from(levelMap.values()).map(async ({ id, level_id, metadata, geomChunks }) => {
        if (geomChunks.length === 0) {
          return { id, grid_id, level_id, metadata, cells: [], n: 0 };
        }
        const chunkResults = await Promise.all(
          geomChunks.map((chunk) => pool_mallas.any(meshQuery, [chunk]))
        );
        const cellSet = new Set();
        for (const rows of chunkResults) {
          for (const r of rows) cellSet.add(r.cell);
        }
        const cells = Array.from(cellSet).sort((a, b) => (a < b ? -1 : 1));
        return { id, grid_id, level_id, metadata, cells, n: cells.length };
      })
    );

    return finish(responseArray);
  } catch (error) {
    debug(error);
    return res.status(500).json({
      message: 'Error interno al obtener datos de uso de suelo'
    });
  }
};

exports.get_sourceinfo = async function (req, res) {
  try {
    const data = await pool.oneOrNone(
      `SELECT
         name,
         description,
         source_url,
         download_url,
         dict_url
       FROM usv_data_source_info
       ORDER BY updated_at DESC, id DESC
       LIMIT 1;`,
      {}
    );

    if (!data) {
      return res.status(404).json({
        message: 'No se encontró información de la fuente de uso de suelo'
      });
    }

    return res.status(200).json({ data });
  } catch (error) {
    debug(error);
    return res.status(500).json({
      message: 'Error interno al obtener información de la fuente de uso de suelo'
    });
  }
};

exports.secuencia = async function (req, res) {
  try {
    const { variableLevel, variableValue, nextVariableLevel } = req.body || {};

    if (!variableLevel || !nextVariableLevel) {
      return res.status(400).json({ message: 'Parámetros requeridos: variableLevel, nextVariableLevel' });
    }

    const current = String(variableLevel).trim().toLowerCase();
    const next = String(nextVariableLevel).trim().toLowerCase();

    // Única transición soportada: usv_<serie> → clase_usv
    if (!current.startsWith('usv') || next !== CATEGORY_LEVEL) {
      return res.status(400).json({ message: `Transición no soportada: ${current} → ${next}` });
    }

    // variableValue = idfuente / source_var_id (ej. "1")
    const rawId = Number(variableValue);
    const sourceVarId = (Number.isInteger(rawId) && rawId > 0) ? rawId : 1;

    const data = await pool.any(
      `SELECT
         c.id::text AS value,
         c.clave || ' - ' || c.label AS label,
         c.clave,
         c.grupo
       FROM usv_categories c
       WHERE c.source_var_id = $1
       ORDER BY c.grupo, c.cat_index`,
      [sourceVarId]
    );

    return res.status(200).json({ data });
  } catch (error) {
    debug(error);
    return res.status(500).json({ message: 'Error interno al obtener secuencia de uso de suelo' });
  }
};
