# middleware_usosuelo

Middleware de uso del suelo y vegetación (INEGI, Serie VII) para exponer servicios compatibles con la especificación SPECIES v3. Sigue el mismo contrato que `middleware_dem`.

## Niveles

`/variables` expone dos niveles por serie:

| id | variable | uso |
|---|---|---|
| `2*n-1` | `usv_serie_vii` | nivel selector (la serie) |
| `2*n` | `clase_usv` | clases de uso de suelo (`usv_categories`) |

## Endpoints

- `GET/POST /usosuelo/`
- `GET/POST /usosuelo/db-health`
- `GET/POST /usosuelo/variables`
- `GET/POST /usosuelo/variables/:id`
  - `q = "usv_serie_vii = <texto>"` devuelve la serie (selector).
  - `q = "idfuente=1; clase_usv=BQ,500010,encino"` devuelve clases por id, `CLAVE` de INEGI o texto de la descripción (sin acentos).
  - Filtros adicionales: `grupo`, `clave`, `layer`, `levels_id`.
- `GET/POST /usosuelo/get-data/:id` con `grid_id`, `levels_id` (ids de `usv_categories`) y opcionalmente `filter_names`/`filter_values` (`clase_usv`, `clave`, `grupo`, `layer`). Lee `usv_cells` y calcula en vivo lo que falte.
- `GET/POST /usosuelo/secuencia` con `variableLevel=usv_serie_vii`, `variableValue=<idfuente>`, `nextVariableLevel=clase_usv`.
- `GET/POST /usosuelo/info`

## Variables de entorno

Copia `.env.example` a `.env` y define credenciales.

## Ejecución

```bash
npm install
npm run dev
```

Servicio disponible en `http://localhost:8080/usosuelo`.
