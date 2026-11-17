import os
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Query
from pathlib import Path
import subprocess
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
from config import settings
import tempfile


app = FastAPI(
    title=settings.APP_NAME,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.ALLOW_ORIGIN,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


BASE_DIR = Path(__file__).resolve().parent.parent

RASTER_PATH = BASE_DIR / "data" / "world.tif"


# Configuración de PostgreSQL
DB_CONFIG = {
    "host": settings.DB_HOST,
    "port": settings.DB_PORT,
    "database": settings.DB_NAME,
    "user": settings.DB_USER,
    "password": settings.DB_PASSWORD,
}


@app.get("/")
def root():
    return {
        "app": settings.ALLOW_ORIGIN,
    }


@app.post("/rasters/import")
def import_raster():

    if not RASTER_PATH.is_file():
        raise HTTPException(
            status_code=404, detail=f"No se encuentra el raster: {RASTER_PATH}"
        )

    env = os.environ.copy()
    env["PGPASSWORD"] = DB_CONFIG["password"]

    raster_process = None

    try:
        with tempfile.TemporaryDirectory() as temp_dir:

            filtered_path = Path(temp_dir) / "raster_filtered.tif"

            # 1. Convertir los valores 0 en NoData
            # Se conservan las elevaciones positivas y la resolución original.
            subprocess.run(
                [
                    "gdal_translate",
                    "-a_nodata",
                    "0",
                    "-co",
                    "TILED=YES",
                    "-co",
                    "COMPRESS=DEFLATE",
                    str(RASTER_PATH),
                    str(filtered_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            # 2. Generar SQL mediante raster2pgsql
            raster_process = subprocess.Popen(
                [
                    "raster2pgsql",
                    "-c",
                    "-s",
                    "4326",
                    "-I",
                    "-C",
                    "-M",
                    "-F",
                    "-t",
                    "256x256",
                    str(filtered_path),
                    "public.mapa_raster",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # 3. Importar el raster en PostGIS
            result = subprocess.run(
                [
                    "psql",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-h",
                    DB_CONFIG["host"],
                    "-p",
                    str(DB_CONFIG["port"]),
                    "-U",
                    DB_CONFIG["user"],
                    "-d",
                    DB_CONFIG["database"],
                ],
                stdin=raster_process.stdout,
                capture_output=True,
                text=True,
                env=env,
            )

            raster_process.stdout.close()

            raster_error = raster_process.stderr.read()
            raster_code = raster_process.wait()

            if result.returncode != 0 or raster_code != 0:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "message": "Error al importar el raster",
                        "postgres_error": result.stderr,
                        "raster_error": raster_error,
                    },
                )

            # 4. Eliminar teselas completamente NoData
            cleanup_sql = """
                DELETE FROM public.mapa_raster
                WHERE ST_BandIsNoData(rast, 1, TRUE);
            """

            cleanup_result = subprocess.run(
                [
                    "psql",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-h",
                    DB_CONFIG["host"],
                    "-p",
                    str(DB_CONFIG["port"]),
                    "-U",
                    DB_CONFIG["user"],
                    "-d",
                    DB_CONFIG["database"],
                    "-c",
                    cleanup_sql,
                ],
                capture_output=True,
                text=True,
                env=env,
            )

            if cleanup_result.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "message": "Error eliminando teselas vacías",
                        "postgres_error": cleanup_result.stderr,
                    },
                )

            # 5. Actualizar estadísticas de la tabla
            subprocess.run(
                [
                    "psql",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-h",
                    DB_CONFIG["host"],
                    "-p",
                    str(DB_CONFIG["port"]),
                    "-U",
                    DB_CONFIG["user"],
                    "-d",
                    DB_CONFIG["database"],
                    "-c",
                    "ANALYZE public.mapa_raster;",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            return {
                "message": "Raster importado y optimizado correctamente",
                "file": RASTER_PATH.name,
                "table": "public.mapa_raster",
                "srid": 4326,
                "nodata_value": 0,
                "tile_size": "256x256",
                "empty_tiles_removed": True,
            }

    except FileNotFoundError as e:
        raise HTTPException(
            status_code=500, detail=f"No se encuentra una herramienta necesaria: {e}"
        )

    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500, detail=f"Error ejecutando proceso: {e.stderr}"
        )

    finally:
        if raster_process is not None and raster_process.poll() is None:
            raster_process.kill()
            raster_process.wait()


@app.get("/terrain/download")
def download_terrain():

    if not RASTER_PATH.is_file():
        raise HTTPException(
            status_code=404, detail="No se encuentra el archivo GeoTIFF original"
        )

    return FileResponse(
        path=str(RASTER_PATH), media_type="image/tiff", filename="world.tif"
    )


@app.get("/terrain/grid")
def get_terrain_grid(
    min_lon: float = Query(-180, ge=-180, le=180),
    min_lat: float = Query(-60, ge=-90, le=90),
    max_lon: float = Query(180, ge=-180, le=180),
    max_lat: float = Query(60, ge=-90, le=90),
    width: int = Query(360, ge=2, le=512),
    height: int = Query(120, ge=2, le=512),
):
    conn = None

    # Comprobar los límites geográficos
    if min_lon >= max_lon or min_lat >= max_lat:
        raise HTTPException(
            status_code=400, detail="Los límites geográficos no son válidos"
        )

    try:
        conn = psycopg2.connect(**DB_CONFIG)

        with conn.cursor(cursor_factory=RealDictCursor) as cursor:

            cursor.execute(
                """
                WITH bounds AS (
                    SELECT
                        ST_MakeEnvelope(
                            %s, %s, %s, %s, 4326
                        ) AS geom,
                        %s AS xmin,
                        %s AS ymin,
                        %s AS xmax,
                        %s AS ymax
                ),

                mosaic AS (
                    SELECT ST_Union(r.rast) AS rast
                    FROM public.mapa_raster r
                    CROSS JOIN bounds b
                    WHERE ST_ConvexHull(r.rast) && b.geom
                ),

                grid AS (
                    SELECT
                        row_idx,
                        col_idx,

                        b.xmin +
                        (col_idx + 0.5) *
                        (b.xmax - b.xmin) / %s AS lon,

                        b.ymax -
                        (row_idx + 0.5) *
                        (b.ymax - b.ymin) / %s AS lat

                    FROM bounds b

                    CROSS JOIN generate_series(0, %s) AS row_idx
                    CROSS JOIN generate_series(0, %s) AS col_idx
                )

                SELECT
                    g.row_idx,
                    g.col_idx,
                    ST_Value(
                        m.rast,
                        1,
                        ST_SetSRID(
                            ST_MakePoint(g.lon, g.lat),
                            4326
                        ),
                        true
                    ) AS elevation

                FROM mosaic m
                CROSS JOIN grid g

                ORDER BY g.row_idx, g.col_idx;
            """,
                (
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    width,
                    height,
                    height - 1,
                    width - 1,
                ),
            )

            pixels = cursor.fetchall()

        # Construir matriz de elevaciones
        elevations = [[None for _ in range(width)] for _ in range(height)]

        for pixel in pixels:
            row = pixel["row_idx"]
            col = pixel["col_idx"]

            elevations[row][col] = pixel["elevation"]

        return {
            "crs": "EPSG:4326",
            "bounds": {
                "min_lon": min_lon,
                "min_lat": min_lat,
                "max_lon": max_lon,
                "max_lat": max_lat,
            },
            "width": width,
            "height": height,
            "elevations": elevations,
        }

    except psycopg2.Error as e:
        raise HTTPException(
            status_code=500, detail=f"Error consultando PostGIS: {str(e)}"
        )

    finally:
        if conn:
            conn.close()


@app.get("/rasters")
def get_rasters():

    conn = None

    try:
        conn = psycopg2.connect(**DB_CONFIG)

        with conn.cursor(cursor_factory=RealDictCursor) as cursor:

            cursor.execute("""
                SELECT
                    COUNT(*) AS total_tiles,

                    COUNT(DISTINCT ST_SRID(rast)) AS srid_count,
                    MIN(ST_SRID(rast)) AS min_srid,
                    MAX(ST_SRID(rast)) AS max_srid,

                    SUM(
                        ST_Width(rast) * ST_Height(rast)
                    ) AS total_pixels,

                    MIN(ST_UpperLeftX(rast)) AS min_lon,

                    MAX(
                        ST_UpperLeftX(rast) +
                        ST_Width(rast) * ST_ScaleX(rast)
                    ) AS max_lon,

                    MIN(
                        ST_UpperLeftY(rast) +
                        ST_Height(rast) * ST_ScaleY(rast)
                    ) AS min_lat,

                    MAX(ST_UpperLeftY(rast)) AS max_lat,

                    MIN(ST_ScaleX(rast)) AS pixel_size_x,
                    MIN(ST_ScaleY(rast)) AS pixel_size_y,

                    MIN(stats.min) AS min_elevation,
                    MAX(stats.max) AS max_elevation,

                    SUM(stats.sum) /
                    NULLIF(SUM(stats.count), 0) AS mean_elevation,

                    SUM(stats.count) AS valid_pixels

                FROM public.mapa_raster r

                CROSS JOIN LATERAL (
                    SELECT (ST_SummaryStats(r.rast, 1, true)).*
                ) AS stats;
            """)

            raster_info = cursor.fetchone()

        return {"message": "Información del raster", "raster": raster_info}

    except psycopg2.Error as e:
        raise HTTPException(
            status_code=500, detail=f"Error consultando PostGIS: {str(e)}"
        )

    finally:
        if conn:
            conn.close()


@app.get("/terrain/tile/{tile_x}/{tile_y}")
def get_terrain_tile(
    tile_x: int,
    tile_y: int,
    width: int = Query(30, ge=2, le=256),
    height: int = Query(30, ge=2, le=256),
):
    TILE_SIZE = 10

    # Comprobar índices de tesela
    if not (0 <= tile_x < 36):
        raise HTTPException(400, "tile_x debe estar entre 0 y 35")

    if not (0 <= tile_y < 12):
        raise HTTPException(400, "tile_y debe estar entre 0 y 11")

    # Calcular límites geográficos
    min_lon = -180 + tile_x * TILE_SIZE
    max_lon = min_lon + TILE_SIZE

    min_lat = -60 + tile_y * TILE_SIZE
    max_lat = min_lat + TILE_SIZE

    conn = None

    try:
        conn = psycopg2.connect(**DB_CONFIG)

        with conn.cursor(cursor_factory=RealDictCursor) as cursor:

            cursor.execute(
                """
                WITH bounds AS (
                    SELECT
                        ST_MakeEnvelope(
                            %s, %s, %s, %s, 4326
                        ) AS geom
                ),

                mosaic AS (
                    SELECT ST_Union(r.rast) AS rast
                    FROM public.mapa_raster r
                    CROSS JOIN bounds b
                    WHERE ST_ConvexHull(r.rast) && b.geom
                ),

                grid AS (
                    SELECT
                        row_idx,
                        col_idx,

                        %s + (col_idx + 0.5) * (%s - %s) / %s AS lon,

                        %s - (row_idx + 0.5) * (%s - %s) / %s AS lat

                    FROM
                        generate_series(0, %s - 1) AS row_idx,
                        generate_series(0, %s - 1) AS col_idx
                )

                SELECT
                    g.row_idx,
                    g.col_idx,

                    ST_Value(
                        m.rast,
                        1,
                        ST_SetSRID(
                            ST_MakePoint(g.lon, g.lat),
                            4326
                        ),
                        true
                    ) AS elevation

                FROM mosaic m
                CROSS JOIN grid g

                ORDER BY g.row_idx, g.col_idx;
            """,
                (
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    min_lon,
                    max_lon,
                    min_lon,
                    width,
                    max_lat,
                    max_lat,
                    min_lat,
                    height,
                    height,
                    width,
                ),
            )

            pixels = cursor.fetchall()

        elevations = [[None for _ in range(width)] for _ in range(height)]

        for pixel in pixels:
            elevations[pixel["row_idx"]][pixel["col_idx"]] = pixel["elevation"]

        return {
            "tile_x": tile_x,
            "tile_y": tile_y,
            "crs": "EPSG:4326",
            "bounds": {
                "min_lon": min_lon,
                "min_lat": min_lat,
                "max_lon": max_lon,
                "max_lat": max_lat,
            },
            "width": width,
            "height": height,
            "elevations": elevations,
        }

    except psycopg2.Error as e:
        raise HTTPException(500, detail=f"Error consultando PostGIS: {str(e)}")

    finally:
        if conn:
            conn.close()


@app.get("/terrain/lod/{level}/{tile_x}/{tile_y}")
def get_terrain_lod_tile(level: int, tile_x: int, tile_y: int):
    # Configuración de cada nivel
    LOD_CONFIG = {
        0: {"tile_size": 30, "width": 30, "height": 30, "max_x": 12, "max_y": 4},
        1: {"tile_size": 10, "width": 30, "height": 30, "max_x": 36, "max_y": 12},
        2: {"tile_size": 2, "width": 60, "height": 60, "max_x": 180, "max_y": 60},
    }

    if level not in LOD_CONFIG:
        raise HTTPException(400, "Nivel LOD no válido")

    config = LOD_CONFIG[level]

    if not (0 <= tile_x < config["max_x"]):
        raise HTTPException(400, "tile_x fuera de rango")

    if not (0 <= tile_y < config["max_y"]):
        raise HTTPException(400, "tile_y fuera de rango")

    size = config["tile_size"]
    width = config["width"]
    height = config["height"]

    min_lon = -180 + tile_x * size
    max_lon = min_lon + size

    min_lat = -60 + tile_y * size
    max_lat = min_lat + size

    conn = None

    try:
        conn = psycopg2.connect(**DB_CONFIG)

        with conn.cursor(cursor_factory=RealDictCursor) as cursor:

            cursor.execute(
                """
                WITH bounds AS (
                    SELECT
                        ST_MakeEnvelope(
                            %s, %s, %s, %s, 4326
                        ) AS geom
                ),

                mosaic AS (
                    SELECT ST_Union(r.rast) AS rast
                    FROM public.mapa_raster r
                    CROSS JOIN bounds b
                    WHERE ST_ConvexHull(r.rast) && b.geom
                ),

                grid AS (
                    SELECT
                        row_idx,
                        col_idx,

                        %s + (col_idx + 0.5) *
                        (%s - %s) / %s AS lon,

                        %s - (row_idx + 0.5) *
                        (%s - %s) / %s AS lat

                    FROM
                        generate_series(0, %s - 1) AS row_idx,
                        generate_series(0, %s - 1) AS col_idx
                )

                SELECT
                    g.row_idx,
                    g.col_idx,

                    ST_Value(
                        m.rast,
                        1,
                        ST_SetSRID(
                            ST_MakePoint(g.lon, g.lat),
                            4326
                        ),
                        true
                    ) AS elevation

                FROM mosaic m
                CROSS JOIN grid g

                ORDER BY g.row_idx, g.col_idx;
            """,
                (
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    min_lon,
                    max_lon,
                    min_lon,
                    width,
                    max_lat,
                    max_lat,
                    min_lat,
                    height,
                    height,
                    width,
                ),
            )

            pixels = cursor.fetchall()

        elevations = [[None for _ in range(width)] for _ in range(height)]

        for pixel in pixels:
            elevations[pixel["row_idx"]][pixel["col_idx"]] = pixel["elevation"]

        return {
            "level": level,
            "tile_x": tile_x,
            "tile_y": tile_y,
            "tile_size_degrees": size,
            "crs": "EPSG:4326",
            "bounds": {
                "min_lon": min_lon,
                "min_lat": min_lat,
                "max_lon": max_lon,
                "max_lat": max_lat,
            },
            "width": width,
            "height": height,
            "elevations": elevations,
        }

    except psycopg2.Error as e:
        raise HTTPException(500, detail=f"Error consultando PostGIS: {str(e)}")

    finally:
        if conn:
            conn.close()
