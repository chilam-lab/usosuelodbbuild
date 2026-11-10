#!/usr/bin/env python
"""
Precalcula usv_cells (celdas por clase de uso de suelo y por malla) para que
get-data del middleware no tenga que cruzar en vivo los polígonos de cada clase
con la malla en cada análisis.

Por cada clase (usv_categories.id) se copian sus polígonos (subdivididos para que el
índice espacial rinda) a una tabla temporal con índice en meshandregions_db y
se cruzan con cada resolución completa. Las celdas de cada región
(cat_grid.region_id) se calculan una sola vez por grid_id, con el mismo
criterio que el middleware (ST_Intersects con el border de
grid_geojson_<res>_aoi).

Fases: primero mallas regulares (64/32/16/8 km), luego las de México
(state/mun/cue/ageb). Es reanudable: solo calcula los (cid, grid_id) que aún no
están en usv_cells.

Uso:
  python precompute_usv_cells.py [--phase regular|irregular|all] [--cids 500000,500001]
                                 [--grids 1,13] [--force]
"""
import os
import sys
import time
import logging
import argparse

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

REGULAR = ['64km', '32km', '16km', '8km']
IRREGULAR = ['state', 'mun', 'cue', 'ageb']

logging.basicConfig(format='[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S', level=logging.INFO)
log = logging.getLogger('usv_cells')


def connect_usv():
    return psycopg2.connect(
        dbname=os.getenv('DBNICHENAME', 'usv_db'), host=os.getenv('DBNICHEHOST'), port=os.getenv('DBNICHEPORT'),
        user=os.getenv('DBNICHEUSER'), password=os.getenv('DBNICHEPASSWD'),
        application_name='usv_cells_precompute')


def connect_mesh():
    return psycopg2.connect(
        dbname=os.getenv('DBMESHNAME'), host=os.getenv('DBMESHHOST'), port=os.getenv('DBMESHPORT'),
        user=os.getenv('DBMESHUSER'), password=os.getenv('DBMESHPASSWD'),
        application_name='usv_cells_precompute', options='-c work_mem=64MB')


def load_grids(mesh, resolutions, only_grids):
    """{resolution: {'table', 'view', 'grids': [(grid_id, region_id)]}}"""
    with mesh.cursor() as cur:
        cur.execute("""SELECT grid_id, region_id, resolution, table_cell_name, table_view_name
                       FROM cat_grid WHERE resolution = ANY(%s) ORDER BY grid_id""", (resolutions,))
        out = {}
        for grid_id, region_id, res, table, view in cur.fetchall():
            if only_grids and grid_id not in only_grids:
                continue
            entry = out.setdefault(res, {'table': table, 'view': view, 'grids': []})
            entry['grids'].append((grid_id, region_id))
    return {r: out[r] for r in resolutions if r in out}


def region_cells(mesh, table, view, res, region_id):
    """Celdas de la malla dentro de la región, igual que el CTE regionarea del middleware."""
    with mesh.cursor() as cur:
        cur.execute(f"""
            SELECT array_agg(DISTINCT g.gridid_{res})
            FROM {table} g
            JOIN {view} vg ON ST_Intersects(g.the_geom, vg.border)
            WHERE vg.region_id = %s""", (region_id,))
        return set(cur.fetchone()[0] or [])


def load_categories(usv, only_cids):
    with usv.cursor() as cur:
        cur.execute("""SELECT c.id, c.clave, 'usv_' || s.series
                       FROM usv_categories c JOIN usv_source_vars s ON s.id = c.source_var_id
                       ORDER BY c.id""")
        rows = cur.fetchall()
    return [r for r in rows if not only_cids or r[0] in only_cids]


def done_pairs(usv):
    with usv.cursor() as cur:
        cur.execute("SELECT cid, grid_id FROM usv_cells")
        return set(cur.fetchall())


def load_category_polygons(usv, mesh, table, cid):
    """Copia los polígonos de la clase, subdivididos, a la tabla temporal bin_polys de mesh."""
    with usv.cursor() as cur:
        cur.execute(f"""SELECT ST_AsEWKB(ST_SetSRID(the_geom, 4326))
                        FROM {psycopg2.extensions.quote_ident(table, cur)}
                        WHERE category_id = %s""", (cid,))
        polys = [(bytes(r[0]),) for r in cur.fetchall()]
    with mesh.cursor() as cur:
        cur.execute("TRUNCATE bin_polys_raw, bin_polys")
        psycopg2.extras.execute_values(cur, "INSERT INTO bin_polys_raw (geom) VALUES %s", polys,
                                       template="(ST_GeomFromEWKB(%s))", page_size=2000)
        cur.execute("INSERT INTO bin_polys (geom) SELECT ST_Subdivide(geom, 64) FROM bin_polys_raw")
        cur.execute("ANALYZE bin_polys")
    return len(polys)


def bin_cells(mesh, table, res):
    with mesh.cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT g.gridid_{res}
            FROM bin_polys p
            JOIN {table} g ON ST_Intersects(g.the_geom, p.geom)""")
        return {r[0] for r in cur.fetchall()}


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument('--phase', choices=['regular', 'irregular', 'all'], default='all')
    ap.add_argument('--cids', help='lista de usv_categories.id separada por comas (prueba)')
    ap.add_argument('--grids', help='lista de grid_id separada por comas (prueba)')
    ap.add_argument('--force', action='store_true', help='recalcular aunque ya exista en usv_cells')
    args = ap.parse_args()

    only_cids = {int(x) for x in args.cids.split(',')} if args.cids else None
    only_grids = {int(x) for x in args.grids.split(',')} if args.grids else None
    phases = {'regular': [('regulares', REGULAR)], 'irregular': [('irregulares', IRREGULAR)],
              'all': [('regulares', REGULAR), ('irregulares', IRREGULAR)]}[args.phase]

    usv = connect_usv()
    usv.autocommit = True
    mesh = connect_mesh()
    mesh.autocommit = True

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sql', '04_create_usv_cells.sql')) as f:
        usv.cursor().execute(f.read())
    mesh.cursor().execute("CREATE TEMP TABLE bin_polys_raw (geom geometry(Geometry, 4326))")
    mesh.cursor().execute("CREATE TEMP TABLE bin_polys (geom geometry(Geometry, 4326))")
    mesh.cursor().execute("CREATE INDEX ON bin_polys USING gist (geom)")

    cats = load_categories(usv, only_cids)

    # Clases que ya no existen en usv_categories (serie recargada sin ellas):
    # sus celdas ya no corresponden a nada.
    with usv.cursor() as cur:
        cur.execute("DELETE FROM usv_cells WHERE cid NOT IN (SELECT id FROM usv_categories)")
        if cur.rowcount:
            log.info(f"Eliminadas {cur.rowcount} filas de usv_cells de clases que ya no existen")

    for phase_name, resolutions in phases:
        grids = load_grids(mesh, resolutions, only_grids)
        all_grid_ids = [gid for g in grids.values() for gid, _ in g['grids']]
        done = set() if args.force else done_pairs(usv)
        pending = [c for c in cats if any((c[0], gid) not in done for gid in all_grid_ids)]
        log.info(f"Fase {phase_name}: mallas={all_grid_ids} clases pendientes={len(pending)}/{len(cats)}")
        if not pending:
            continue

        regions = {}
        for res, g in grids.items():
            for grid_id, region_id in g['grids']:
                t0 = time.time()
                regions[grid_id] = region_cells(mesh, g['table'], g['view'], res, region_id)
                log.info(f"  región grid_id={grid_id} ({res}, region {region_id}): "
                         f"{len(regions[grid_id])} celdas en {time.time() - t0:.1f}s")

        started = time.time()
        for i, (cid, clave, table) in enumerate(pending, 1):
            t0 = time.time()
            npolys = load_category_polygons(usv, mesh, table, cid)
            rows = []
            for res, g in grids.items():
                todo = [(gid, rid) for gid, rid in g['grids'] if (cid, gid) not in done]
                if not todo:
                    continue
                cells = bin_cells(mesh, g['table'], res) if npolys else set()
                for grid_id, _ in todo:
                    rows.append((cid, grid_id, sorted(cells & regions[grid_id])))
            with usv.cursor() as cur:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO usv_cells (cid, grid_id, cells) VALUES %s
                    ON CONFLICT (cid, grid_id) DO UPDATE SET cells = EXCLUDED.cells, updated_at = now()""",
                    rows)
            elapsed = time.time() - started
            eta = elapsed / i * (len(pending) - i) / 3600
            log.info(f"[{phase_name}] {i}/{len(pending)} cid={cid} clave={clave} "
                     f"polígonos={npolys} mallas={len(rows)} tiempo={time.time() - t0:.1f}s eta~{eta:.1f}h")

    log.info("Precalculo USV completo.")


if __name__ == '__main__':
    sys.exit(main())
