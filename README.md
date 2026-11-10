# Uso del Suelo y Vegetación DB Build

Construcción de base de datos geoespacial de uso del suelo y vegetación (INEGI, escala 1:250 000, Serie VII) para México.

Este módulo genera y carga en PostGIS:
- catálogo de fuente y serie,
- catálogo de clases de uso de suelo y vegetación (`usv_categories`),
- capa vectorial de la serie (`usv_serie_vii`),
- metadatos de cobertura por mallas (`available_grids`) usando `meshandregions_db`,
- celdas precalculadas por clase y malla (`usv_cells`).

## Objetivo

Automatizar un pipeline ETL para uso de suelo, con el mismo contrato de servicios que DEM y WorldClim:
1. descarga y descompresión del shapefile nacional de INEGI,
2. carga a PostGIS reproyectando a EPSG:4326 y corrigiendo geometrías inválidas,
3. catálogo de clases a partir de `CLAVE` / `DESCRIPCIO`,
4. enlace de cada polígono con su clase,
5. cálculo de `available_grids` por cruce espacial con mallas/regiones,
6. precálculo de celdas por clase y malla.

## Fuente

- Producto: *Conjunto de datos vectoriales de uso del suelo y vegetación. Escala 1:250 000. Serie VII. Conjunto Nacional* (INEGI, edición 2018).
- Ficha: https://www.inegi.org.mx/app/biblioteca/ficha.html?upc=889463842781
- Descarga (SHP, ~294 MB): `889463842781_s.zip`, contiene `conjunto_de_datos/cdv_usuev250sVII_cnal.shp`.
- 179,099 polígonos, 183 clases (`CLAVE`), proyección cónica conforme de Lambert (ITRF2008).

## Estructura

```text
dbbuild/
├── build_usvdb.py                  # Script principal ETL
├── precompute_usv_cells.py         # Precálculo de celdas por clase y malla (usv_cells)
├── .env                            # Variables de conexión (usv_db y meshandregions_db)
├── data/
│   └── input/
│       ├── 889463842781_s.zip      # Zip de INEGI (se descarga con --download)
│       └── conjunto_de_datos/      # Shapefile extraído
└── sql/
    ├── 01_init_usv_db.sql          # Esquema base (tablas USV)
    ├── 02_create_mesh_fdw.sql      # FDW a meshandregions_db
    ├── 03_update_available_grids.sql # Cálculo de available_grids
    └── 04_create_usv_cells.sql     # Tabla de celdas precalculadas
middleware_usosuelo/                # Servicios SPECIES v3 (ver su README)
```

## Tablas principales

- `usv_data_source_info`: metadatos de la fuente de datos.
- `usv_source_vars`: una fila por serie (`source_code`, `series`, `level_size`, `available_grids`, `filter_fields`).
- `usv_categories`: clases de uso de suelo (`clave`, `label`, `grupo`, `cat_index`, `n_polygons`, `area_km2`). Los ids empiezan en 500000 y se conservan entre corridas (se actualizan por `clave`).
- `usv_serie_vii`: polígonos de la serie con `category_id` hacia `usv_categories`.
- `usv_cells`: celdas por clase (`cid`) y malla (`grid_id`).

### Clases y grupos

- `label` es la `DESCRIPCIO` de INEGI. En vegetación secundaria (`VSA/`, `VSa/`, `VSh/`) se arma con la clase primaria, porque el shape trunca algunas descripciones a 70 caracteres.
- `grupo` agrupa las clases para el front: Bosque, Selva, Matorral, Pastizal, Agricultura, Vegetación secundaria arbórea/arbustiva/herbácea, Otros tipos de vegetación, Asentamientos humanos, Cuerpo de agua, Acuícola y Sin vegetación aparente.

## Prerrequisitos

- Python 3 con `psycopg2` y `python-dotenv`
- GDAL CLI disponible en PATH (`ogr2ogr` ≥ 3.1, por `-makevalid`)
- PostgreSQL + PostGIS accesible (la base `usv_db` debe existir)
- Credenciales en `.env` (ver `dbbuild/.env.example`)

## Ejecución

Desde la carpeta `dbbuild`:

```bash
python3 build_usvdb.py --dry-run
python3 build_usvdb.py --download            # descarga, carga, catálogo, available_grids y usv_cells
python3 build_usvdb.py --skip-load           # rehace catálogo/grids/celdas sin recargar el shape
python3 build_usvdb.py --skip-cells          # sin precálculo de celdas
python3 precompute_usv_cells.py --phase regular   # solo el precálculo (reanudable)
```

## Validación rápida SQL

```sql
select id, source_code, series, level_size, array_length(available_grids, 1) as n_grids
from usv_source_vars;

select grupo, count(*) as clases, sum(n_polygons) as poligonos, round(sum(area_km2)) as km2
from usv_categories
group by grupo
order by km2 desc;

select count(*), count(*) filter (where category_id is null) as sin_clase from usv_serie_vii;

select c.clave, c.label, count(u.*) as mallas
from usv_categories c left join usv_cells u on u.cid = c.id
group by c.id order by c.cat_index limit 20;
```

## Notas operativas

- El script ejecuta `01_init_usv_db.sql` en cada corrida (idempotente).
- `available_grids` se calcula al final usando FDW (`02_create_mesh_fdw.sql` + `03_update_available_grids.sql`).
- Al recargar la serie se reemplaza la tabla `usv_serie_vii`; las clases conservan su id y `usv_cells` se recalcula con `--force`.
- Para agregar otra serie (ej. Serie VIII) cambia el bloque de constantes de `build_usvdb.py`; ambas coexisten en `usv_source_vars` (clave única `source_code + area + series`).

## middleware_usosuelo

`middleware_usosuelo` expone la información bajo el mismo contrato que `middleware_dem`, con el prefijo de rutas `/usosuelo`.

### Variables de entorno (`middleware_usosuelo/.env`)

```env
PORT=

DBNAME=
DBUSER=
DBPWD=
DBHOST=
DBPORT=

DBNAME_MALLAS=
DBUSER_MALLAS=
DBPWD_MALLAS=
DBHOST_MALLAS=
DBPORT_MALLAS=
```

### Ejecución

```bash
cd middleware_usosuelo
npm install
npm run dev
```

Con Docker (puerto 8899):

```bash
docker-compose up -d --build
```

### Endpoints base

- `GET/POST /usosuelo/`
- `GET/POST /usosuelo/db-health`
- `GET/POST /usosuelo/variables`
- `GET/POST /usosuelo/variables/:id`
- `GET/POST /usosuelo/get-data/:id`
- `GET/POST /usosuelo/secuencia`
- `GET/POST /usosuelo/info`
