#!/usr/bin/env python3
# Script ETL para Uso del Suelo y Vegetación (INEGI, escala 1:250 000):
# 1) descarga/descomprime el shapefile nacional de la serie,
# 2) lo carga a PostGIS reproyectado a EPSG:4326 y lo recorta al borde terrestre
#    (aoi_extent, mismo AOI que WorldClim/DEM; el shape trae el mar como agua) en usv_{serie},
# 3) genera el catálogo de clases (usv_categories) a partir de CLAVE/DESCRIPCIO,
# 4) enlaza cada polígono con su clase y calcula available_grids,
# 5) precalcula las celdas por clase y malla (usv_cells).
import argparse
import logging
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
# Estructura de carpetas del proyecto.
DATA_DIR = BASE_DIR / "data"
INPUT_DIR = DATA_DIR / "input"
SQL_DIR = BASE_DIR / "sql"

# Serie VII (edición 2018). Para otra serie basta cambiar este bloque.
SERIES = "serie_vii"
INPUT_ZIP = INPUT_DIR / "889463842781_s.zip"
INPUT_SHAPEFILE = INPUT_DIR / "conjunto_de_datos" / "cdv_usuev250sVII_cnal.shp"
SHAPE_CLAVE_FIELD = "clave"
SHAPE_DESC_FIELD = "descripcio"
# Borde terrestre (Américas) con que se recortan WorldClim y DEM.
AOI_SHAPEFILE = DATA_DIR / "aoi_extent.shp"
AOI_TABLE = "usv_aoi"

# Metadatos que se guardan en catálogo de la DB.
SOURCE_CODE = "inegi_usv_250k_serie_vii"
SOURCE_NAME = "INEGI"
VARIABLE_NAME = "usv_serie_vii"
SCALE = "1:250 000"
EDITION = "2018"
AREA = "Mexico"
DATASET_NAME = "INEGI Uso del Suelo y Vegetación Data Source"
DATASET_DESCRIPTION = (
    "Conjunto de datos vectoriales de uso del suelo y vegetación, escala 1:250 000, Serie VII "
    "(Conjunto Nacional, INEGI); polígonos por clase de uso de suelo y vegetación para consulta espacial."
)
SOURCE_URL = "https://www.inegi.org.mx/app/biblioteca/ficha.html?upc=889463842781"
DOWNLOAD_URL = (
    "https://www.inegi.org.mx/contenidos/productos/prod_serv/contenidos/espanol/bvinegi/productos/"
    "geografia/tematicas/uso_suelo/1_250_000/serie_VII/889463842781_s.zip"
)
DICT_URL = "https://www.inegi.org.mx/temas/usosuelo/"
FILTER_FIELDS = '{"area":"string","series":"string","clase_usv":"string","grupo":"string"}'
USV_LAYER = "usv001"
CATEGORY_ID_START = 500000

CREATE_MESH_FDW_SQL = SQL_DIR / "02_create_mesh_fdw.sql"
UPDATE_AVAILABLE_GRIDS_SQL = SQL_DIR / "03_update_available_grids.sql"
CLIP_TO_LAND_SQL = SQL_DIR / "05_clip_to_land.sql"

# Prefijos de vegetación secundaria: la DESCRIPCIO del shape viene truncada a
# 70 caracteres en algunas de estas clases, así que su etiqueta se arma con la
# de la clase primaria (ej. VSa/SBQ -> "... ARBUSTIVA DE " + label de SBQ).
SECONDARY_PREFIXES = {
    "VSA/": ("VEGETACIÓN SECUNDARIA ARBÓREA DE ", "Vegetación secundaria arbórea"),
    "VSa/": ("VEGETACIÓN SECUNDARIA ARBUSTIVA DE ", "Vegetación secundaria arbustiva"),
    "VSh/": ("VEGETACIÓN SECUNDARIA HERBÁCEA DE ", "Vegetación secundaria herbácea"),
}
# Grupo de las clases primarias por clave exacta; el resto se resuelve por la
# primera letra en category_group().
GROUP_BY_CLAVE = {
    "AH": "Asentamientos humanos",
    "H2O": "Cuerpo de agua",
    "ACUI": "Acuícola",
    "ADV": "Sin vegetación aparente",
    "DV": "Sin vegetación aparente",
    "MK": "Bosque",
    "PT": "Otros tipos de vegetación",
}
GROUP_BY_LETTER = {
    "B": "Bosque",
    "S": "Selva",
    "M": "Matorral",
    "P": "Pastizal",
    "R": "Agricultura",
    "T": "Agricultura",
    "H": "Agricultura",
    "V": "Otros tipos de vegetación",
}


def setup_logger() -> logging.Logger:
    # Logger único y simple para todo el script.
    logger = logging.getLogger("build_usvdb")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def run_cmd(cmd: list[str], logger: logging.Logger, secret: str | None = None) -> None:
    # Ejecuta comandos de sistema (GDAL/OGR) y aborta si fallan.
    shown = " ".join(cmd)
    if secret:
        shown = shown.replace(secret, "****")
    logger.info("Running: %s", shown)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(result.stderr.strip())
        raise RuntimeError(f"Command failed: {shown}")
    if result.stdout.strip():
        logger.info(result.stdout.strip())


def load_sql(path: Path) -> str:
    # Lee script SQL completo desde archivo.
    return path.read_text(encoding="utf-8")


def table_name() -> str:
    return f"usv_{SERIES}"


def raw_table_name() -> str:
    # Shape tal cual lo carga ogr2ogr, antes del recorte terrestre.
    return f"{table_name()}_raw"


def category_group(clave: str) -> str:
    # Grupo legible de una clase (nivel de agrupación para el front).
    for prefix, (_, group) in SECONDARY_PREFIXES.items():
        if clave.startswith(prefix):
            return group
    if clave in GROUP_BY_CLAVE:
        return GROUP_BY_CLAVE[clave]
    return GROUP_BY_LETTER.get(clave[:1].upper(), "Otros tipos de vegetación")


def category_labels(rows: list[tuple[str, str]]) -> dict[str, str]:
    # Etiqueta por clave; corrige las de vegetación secundaria truncadas.
    labels = {clave: desc.strip() for clave, desc in rows}
    for clave in labels:
        for prefix, (text, _) in SECONDARY_PREFIXES.items():
            base = clave[len(prefix):]
            if clave.startswith(prefix) and base in labels:
                labels[clave] = text + labels[base]
    return labels


def download_and_extract(logger: logging.Logger, download: bool) -> None:
    # Descarga el zip de INEGI (si se pide) y extrae el shapefile.
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not INPUT_ZIP.exists():
        if not download:
            raise FileNotFoundError(f"Input zip not found: {INPUT_ZIP} (usa --download)")
        logger.info("Downloading %s", DOWNLOAD_URL)
        request = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request) as resp, open(INPUT_ZIP, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)

    if not INPUT_SHAPEFILE.exists():
        logger.info("Extracting %s", INPUT_ZIP.name)
        with zipfile.ZipFile(INPUT_ZIP) as zf:
            zf.extractall(INPUT_DIR)
    if not INPUT_SHAPEFILE.exists():
        raise FileNotFoundError(f"Shapefile not found after extracting: {INPUT_SHAPEFILE}")


def db_params() -> dict[str, str]:
    return {
        "dbname": os.getenv("DBNICHENAME", "usv_db"),
        "host": os.getenv("DBNICHEHOST", "localhost"),
        "port": os.getenv("DBNICHEPORT", "5432"),
        "user": os.getenv("DBNICHEUSER", "postgres"),
        "password": os.getenv("DBNICHEPASSWD", "postgres"),
    }


def get_db_conn():
    # Conexión PostgreSQL usando variables de entorno de dbbuild/.env.
    import psycopg2

    return psycopg2.connect(**db_params())


def init_db(logger: logging.Logger) -> None:
    # Crea extensión PostGIS y tablas base (usv_source_vars, usv_categories).
    init_sql = load_sql(SQL_DIR / "01_init_usv_db.sql")
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(init_sql)
            # Inicio de ids de clase, solo la primera vez (no pisa ids existentes).
            cur.execute(
                """
                SELECT setval('public.usv_categories_id_seq', %s, false)
                WHERE NOT EXISTS (SELECT 1 FROM usv_categories);
                """,
                (CATEGORY_ID_START,),
            )
    logger.info("DB initialized")


def ogr_load(shapefile: Path, table: str, logger: logging.Logger, geom_name: str = "the_geom") -> None:
    # Carga un shapefile como tabla PostGIS en EPSG:4326.
    params = db_params()
    pg_conn = " ".join(f"{k}={v}" for k, v in params.items())
    run_cmd(
        [
            "ogr2ogr",
            "-f",
            "PostgreSQL",
            f"PG:{pg_conn}",
            str(shapefile),
            "-nln",
            table,
            "-overwrite",
            "-t_srs",
            "EPSG:4326",
            "-makevalid",
            "-lco",
            f"GEOMETRY_NAME={geom_name}",
            "-lco",
            "FID=id",
            "-lco",
            "PRECISION=NO",
            "-nlt",
            "MULTIPOLYGON",
            "--config",
            "PG_USE_COPY",
            "YES",
        ],
        logger,
        secret=params["password"],
    )
    logger.info("Shapefile loaded in table: %s", table)


def load_shapefile_to_postgis(logger: logging.Logger) -> str:
    # Carga el shape de INEGI y el AOI terrestre, y recorta el primero con el segundo.
    if not AOI_SHAPEFILE.exists():
        raise FileNotFoundError(f"AOI shapefile not found: {AOI_SHAPEFILE}")
    ogr_load(INPUT_SHAPEFILE, raw_table_name(), logger)
    ogr_load(AOI_SHAPEFILE, AOI_TABLE, logger, geom_name="geom")

    clip_sql = load_sql(CLIP_TO_LAND_SQL)
    clip_sql = clip_sql.replace("__RAW_TABLE__", raw_table_name())
    clip_sql = clip_sql.replace("__AOI_TABLE__", AOI_TABLE)
    clip_sql = clip_sql.replace("__USV_TABLE__", table_name())
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(clip_sql)
            cur.execute(f"SELECT (SELECT count(*) FROM {raw_table_name()}), (SELECT count(*) FROM {table_name()})")
            n_raw, n_land = cur.fetchone()
    logger.info("Clipped to land: %s -> %s polygons in table: %s", n_raw, n_land, table_name())
    return table_name()


def upsert_source_and_categories(logger: logging.Logger) -> int:
    # Inserta/actualiza la fuente y el catálogo de clases conservando sus ids.
    from psycopg2.extras import execute_values

    table = table_name()
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {SHAPE_CLAVE_FIELD}, max({SHAPE_DESC_FIELD}), count(*),
                       sum(ST_Area(the_geom::geography)) / 1e6
                FROM {table}
                WHERE {SHAPE_CLAVE_FIELD} IS NOT NULL
                GROUP BY {SHAPE_CLAVE_FIELD}
                ORDER BY {SHAPE_CLAVE_FIELD};
                """
            )
            stats = cur.fetchall()
            labels = category_labels([(clave, desc or clave) for clave, desc, _, _ in stats])

            cur.execute(
                """
                INSERT INTO usv_data_source_info (
                    name, description, source_url, download_url, dict_url
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name)
                DO UPDATE SET
                    description = EXCLUDED.description,
                    source_url = EXCLUDED.source_url,
                    download_url = EXCLUDED.download_url,
                    dict_url = EXCLUDED.dict_url,
                    updated_at = NOW()
                RETURNING id;
                """,
                (DATASET_NAME, DATASET_DESCRIPTION, SOURCE_URL, DOWNLOAD_URL, DICT_URL),
            )
            data_source_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO usv_source_vars (
                    data_source_id, source_code, fuente, area, series, descripcion, level_size, available_grids,
                    filter_fields, source_name, variable_name, scale, edition, categories_count, shape_file
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, ARRAY[]::INTEGER[], %s::jsonb, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_code, area, series)
                DO UPDATE SET
                    data_source_id = EXCLUDED.data_source_id,
                    fuente = EXCLUDED.fuente,
                    descripcion = EXCLUDED.descripcion,
                    level_size = EXCLUDED.level_size,
                    available_grids = EXCLUDED.available_grids,
                    filter_fields = EXCLUDED.filter_fields,
                    source_name = EXCLUDED.source_name,
                    variable_name = EXCLUDED.variable_name,
                    scale = EXCLUDED.scale,
                    edition = EXCLUDED.edition,
                    categories_count = EXCLUDED.categories_count,
                    shape_file = EXCLUDED.shape_file,
                    updated_at = NOW()
                RETURNING id;
                """,
                (
                    data_source_id,
                    SOURCE_CODE,
                    "INEGI Uso del suelo y vegetación",
                    AREA,
                    SERIES,
                    f"Uso del suelo y vegetación {SCALE} Serie VII",
                    len(stats),
                    FILTER_FIELDS,
                    SOURCE_NAME,
                    VARIABLE_NAME,
                    SCALE,
                    EDITION,
                    len(stats),
                    INPUT_SHAPEFILE.name,
                ),
            )
            source_var_id = cur.fetchone()[0]

            # A diferencia de dem_bins, las clases se actualizan por clave para
            # conservar su id (usv_cells y análisis guardados lo referencian).
            execute_values(
                cur,
                """
                INSERT INTO usv_categories (source_var_id, cat_index, clave, label, grupo, layer, n_polygons, area_km2)
                VALUES %s
                ON CONFLICT (source_var_id, clave)
                DO UPDATE SET
                    cat_index = EXCLUDED.cat_index,
                    label = EXCLUDED.label,
                    grupo = EXCLUDED.grupo,
                    layer = EXCLUDED.layer,
                    n_polygons = EXCLUDED.n_polygons,
                    area_km2 = EXCLUDED.area_km2,
                    updated_at = NOW()
                """,
                [
                    (source_var_id, idx, clave, labels[clave], category_group(clave), USV_LAYER, n, area)
                    for idx, (clave, _, n, area) in enumerate(stats, start=1)
                ],
            )
            cur.execute(
                "DELETE FROM usv_categories WHERE source_var_id = %s AND NOT (clave = ANY(%s))",
                (source_var_id, [clave for clave, *_ in stats]),
            )

    logger.info("Inserted source metadata and %s categories", len(stats))
    return source_var_id


def link_polygons(source_var_id: int, logger: logging.Logger) -> None:
    # Enriquece la tabla espacial con la clase de cada polígono y crea índices.
    table = table_name()
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                ALTER TABLE {table}
                ADD COLUMN IF NOT EXISTS source_code TEXT,
                ADD COLUMN IF NOT EXISTS source_var_id INTEGER,
                ADD COLUMN IF NOT EXISTS category_id BIGINT,
                ADD COLUMN IF NOT EXISTS cat_index INTEGER;
                """
            )
            cur.execute(
                f"""
                UPDATE {table} p
                SET
                    source_code = %s,
                    source_var_id = c.source_var_id,
                    category_id = c.id,
                    cat_index = c.cat_index
                FROM usv_categories c
                WHERE c.source_var_id = %s
                  AND c.clave = p.{SHAPE_CLAVE_FIELD};
                """,
                (SOURCE_CODE, source_var_id),
            )
            cur.execute(f"DELETE FROM {table} WHERE category_id IS NULL OR the_geom IS NULL")
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_geom ON {table} USING GIST (the_geom)")
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_category ON {table} (category_id)")
            cur.execute(f"ANALYZE {table}")

    logger.info("Polygons linked to categories in table: %s", table)


def configure_mesh_fdw(logger: logging.Logger) -> bool:
    # Configura FDW hacia meshandregions_db para consultar cat_grid y mallas.
    required = {k: os.getenv(k) for k in ("DBMESHNAME", "DBMESHHOST", "DBMESHPORT", "DBMESHUSER", "DBMESHPASSWD")}
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.warning("Skipping available_grids: missing env vars: %s", ", ".join(missing))
        return False

    mesh_fdw_sql = load_sql(CREATE_MESH_FDW_SQL)
    mesh_fdw_sql = mesh_fdw_sql.replace("__MESHDB_HOST__", required["DBMESHHOST"])
    mesh_fdw_sql = mesh_fdw_sql.replace("__MESHDB_PORT__", required["DBMESHPORT"])
    mesh_fdw_sql = mesh_fdw_sql.replace("__MESHDB_NAME__", required["DBMESHNAME"])
    mesh_fdw_sql = mesh_fdw_sql.replace("__MESHDB_USER__", required["DBMESHUSER"])
    mesh_fdw_sql = mesh_fdw_sql.replace("__MESHDB_PASS__", required["DBMESHPASSWD"].replace("'", "''"))

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(mesh_fdw_sql)
    logger.info("FDW configured for mesh DB")
    return True


def update_available_grids(source_var_id: int, logger: logging.Logger) -> None:
    # Calcula available_grids a partir de la intersección de los polígonos con las mallas.
    update_sql = load_sql(UPDATE_AVAILABLE_GRIDS_SQL)
    update_sql = update_sql.replace("__USV_TABLE__", table_name())
    update_sql = update_sql.replace("__SOURCE_VAR_ID__", str(source_var_id))

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(update_sql)
    logger.info("available_grids updated for source_var_id=%s", source_var_id)


def precompute_cells(logger: logging.Logger) -> None:
    # Preconteos: celdas de cada clase (usv_categories.id) por malla, en usv_cells.
    # El middleware (get-data) las lee en vez de cruzar los polígonos en vivo.
    # --force porque el build recarga los polígonos aunque los ids de clase se conserven.
    missing = [k for k in ("DBMESHNAME", "DBMESHHOST", "DBMESHPORT", "DBMESHUSER", "DBMESHPASSWD") if not os.getenv(k)]
    if missing:
        logger.warning("Skipping usv_cells precompute: missing env vars: %s", ", ".join(missing))
        return
    script = BASE_DIR / "precompute_usv_cells.py"
    # Sin run_cmd: ese captura la salida y aquí interesa ver el avance en vivo.
    logger.info("Running: %s --force", script)
    subprocess.run([sys.executable, str(script), "--force"], check=True)
    logger.info("usv_cells precomputed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build land use (INEGI USV) database from shapefile")
    parser.add_argument("--download", action="store_true", help="Download the INEGI zip if it is not in data/input")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration and exit")
    parser.add_argument("--skip-load", action="store_true", help="Reuse the polygon table already loaded")
    parser.add_argument("--skip-cells", action="store_true", help="Do not precompute usv_cells at the end")
    return parser.parse_args()


def main() -> int:
    # Orquestador principal del pipeline.
    logger = setup_logger()
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.error("python-dotenv is required. Activate your virtualenv first.")
        return 1

    load_dotenv(BASE_DIR / ".env")
    args = parse_args()

    # Validación de prerequisitos sin ejecutar procesamiento pesado.
    if args.dry_run:
        logger.info("Dry run OK")
        logger.info("Input zip: %s (%s)", INPUT_ZIP, "ok" if INPUT_ZIP.exists() else "falta")
        logger.info("Shapefile: %s (%s)", INPUT_SHAPEFILE, "ok" if INPUT_SHAPEFILE.exists() else "falta")
        logger.info("Target table: %s", table_name())
        return 0

    if not args.skip_load:
        download_and_extract(logger, args.download)
        load_shapefile_to_postgis(logger)
    init_db(logger)
    source_var_id = upsert_source_and_categories(logger)
    link_polygons(source_var_id, logger)
    if configure_mesh_fdw(logger):
        update_available_grids(source_var_id, logger)
    if args.skip_cells:
        logger.info("Skipping usv_cells precompute (--skip-cells)")
    else:
        precompute_cells(logger)

    logger.info("USV build completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
