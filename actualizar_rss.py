import os
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


URL_BASE = "https://www.rovi.es"
URL_NOTICIAS = "https://www.rovi.es/es/noticias"

ARCHIVO_RSS = Path("rss.xml")
ZONA_HORARIA = ZoneInfo("Europe/Madrid")
MAX_ARTICULOS = 3000

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Referer": URL_BASE + "/",
}


def dentro_del_horario():
    """
    Las ejecuciones automáticas funcionan:
    - De lunes a viernes.
    - Cada hora.
    - Desde las 07:00 hasta las 19:59.
    - Hora peninsular española.

    Las ejecuciones manuales funcionan cualquier día y hora.
    """
    evento = os.environ.get("GITHUB_EVENT_NAME", "")

    if evento == "workflow_dispatch":
        print("Ejecución manual: se ignora el límite horario.")
        return True

    ahora = datetime.now(ZONA_HORARIA)
    print(f"Hora española: {ahora:%Y-%m-%d %H:%M:%S %Z}")

    # Monday=0 ... Saturday=5, Sunday=6
    if ahora.weekday() >= 5:
        print("Fin de semana: no se actualiza el RSS.")
        return False

    if not 7 <= ahora.hour <= 19:
        print("Fuera del horario permitido: 07:00-19:59.")
        return False

    return True


def limpiar_texto(valor):
    if valor is None:
        return ""

    return " ".join(str(valor).split()).strip()


def normalizar_url(url):
    url = limpiar_texto(url)

    if not url:
        return ""

    return urljoin(URL_BASE, url).split("#")[0]


def pertenece_a_rovi(url):
    try:
        dominio = urlparse(url).netloc.lower()

        return dominio in ("rovi.es", "www.rovi.es")

    except ValueError:
        return False


def es_enlace_noticia(url):
    try:
        if not pertenece_a_rovi(url):
            return False

        ruta = urlparse(url).path.lower().rstrip("/")

        if not ruta.startswith("/es/noticias/"):
            return False

        if ruta == "/es/noticias":
            return False

        extensiones_excluidas = (
            ".pdf",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".jpg",
            ".jpeg",
            ".png",
        )

        return not ruta.endswith(extensiones_excluidas)

    except ValueError:
        return False


def es_documento(url):
    try:
        ruta = urlparse(url).path.lower()

        return ruta.endswith(
            (".pdf", ".doc", ".docx", ".xls", ".xlsx")
        )

    except ValueError:
        return False


def descargar_pagina():
    ultimo_error = None

    for intento in range(1, 4):
        try:
            respuesta = requests.get(
                URL_NOTICIAS,
                headers=CABECERAS,
                timeout=45,
                allow_redirects=True,
            )
            respuesta.raise_for_status()

            if not respuesta.text.strip():
                raise RuntimeError("La web se descargó vacía.")

            print(
                f"Página descargada: {len(respuesta.content)} bytes."
            )

            return respuesta.text

        except Exception as error:
            ultimo_error = error
            print(f"Intento {intento}/3 fallido: {error}")

    raise RuntimeError(
        f"No se pudo descargar la web de ROVI: {ultimo_error}"
    )


def convertir_fecha(valor):
    coincidencia = re.search(
        r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
        limpiar_texto(valor),
    )

    if coincidencia:
        dia, mes, anio = map(int, coincidencia.groups())

        try:
            fecha = datetime(
                anio,
                mes,
                dia,
                12,
                0,
                tzinfo=ZONA_HORARIA,
            )

            return format_datetime(
                fecha.astimezone(timezone.utc)
            )

        except ValueError:
            pass

    return format_datetime(datetime.now(timezone.utc))


def buscar_contenedor(enlace_html):
    contenedor = enlace_html

    for _ in range(8):
        if contenedor.parent is None:
            break

        contenedor = contenedor.parent
        contenido = contenedor.get_text(" ", strip=True)

        tiene_fecha = bool(
            re.search(
                r"\b\d{1,2}/\d{1,2}/\d{4}\b",
                contenido,
            )
        )

        noticias = {
            normalizar_url(enlace.get("href")).rstrip("/")
            for enlace in contenedor.find_all("a", href=True)
            if es_enlace_noticia(
                normalizar_url(enlace.get("href"))
            )
        }

        if tiene_fecha and len(noticias) <= 1:
            return contenedor

    return enlace_html.parent or enlace_html


def obtener_fecha(contenedor):
    contenido = contenedor.get_text(" ", strip=True)

    coincidencia = re.search(
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
        contenido,
    )

    if coincidencia:
        return convertir_fecha(coincidencia.group(0))

    return format_datetime(datetime.now(timezone.utc))


def obtener_titulo(enlace_html, contenedor):
    titulo = limpiar_texto(
        enlace_html.get_text(" ", strip=True)
    )

    genericos = {
        "descargar",
        "leer más",
        "saber más",
        "ver noticia",
        "actualidad rovi",
    }

    if titulo.lower() in genericos:
        titulo = ""

    if 15 <= len(titulo) <= 500:
        return titulo

    for selector in (
        "h1",
        "h2",
        "h3",
        "h4",
        ".title",
        ".titulo",
        ".field--name-title",
    ):
        elemento = contenedor.select_one(selector)

        if not elemento:
            continue

        titulo = limpiar_texto(
            elemento.get_text(" ", strip=True)
        )

        if (
            15 <= len(titulo) <= 500
            and titulo.lower() not in genericos
        ):
            return titulo

    return ""


def obtener_resumen(contenedor, titulo):
    candidatos = []

    for elemento in contenedor.find_all(
        ["p", "h2", "h3", "h4"],
    ):
        contenido = limpiar_texto(
            elemento.get_text(" ", strip=True)
        )

        if not contenido or contenido == titulo:
            continue

        if contenido.lower() in (
            "descargar",
            "leer más",
            "saber más",
            "ver noticia",
            "actualidad rovi",
        ):
            continue

        if re.fullmatch(
            r"\d{1,2}/\d{1,2}/\d{4}",
            contenido,
        ):
            continue

        if 30 <= len(contenido) <= 1000:
            candidatos.append(contenido)

    if candidatos:
        candidatos.sort(key=len)
        return candidatos[0]

    return ""


def obtener_imagen(contenedor):
    imagen = contenedor.find("img")

    if not imagen:
        return ""

    for atributo in (
        "data-src",
        "data-lazy-src",
        "data-original",
        "src",
    ):
        url = normalizar_url(imagen.get(atributo))

        if url and not url.startswith("data:"):
            return url

    srcset = limpiar_texto(
        imagen.get("data-srcset")
        or imagen.get("srcset")
    )

    if srcset:
        primera = srcset.split(",")[0].strip().split(" ")[0]
        return normalizar_url(primera)

    return ""


def obtener_documento(contenedor):
    for enlace_html in contenedor.find_all("a", href=True):
        url = normalizar_url(enlace_html.get("href"))
        etiqueta = limpiar_texto(
            enlace_html.get_text(" ", strip=True)
        ).lower()

        if es_documento(url):
            return url

        if "descargar" in etiqueta and url:
            return url

    return ""


def extraer_noticias():
    html = descargar_pagina()
    sopa = BeautifulSoup(html, "html.parser")

    articulos = []
    enlaces_vistos = set()

    for enlace_html in sopa.find_all("a", href=True):
        enlace = normalizar_url(enlace_html.get("href"))

        if not es_enlace_noticia(enlace):
            continue

        enlace = enlace.split("?")[0].rstrip("/")

        if enlace in enlaces_vistos:
            continue

        contenedor = buscar_contenedor(enlace_html)
        titulo = obtener_titulo(enlace_html, contenedor)

        if not titulo:
            continue

        fecha = obtener_fecha(contenedor)
        resumen = obtener_resumen(contenedor, titulo)
        imagen = obtener_imagen(contenedor)
        documento = obtener_documento(contenedor)

        descripcion = ""

        if resumen:
            descripcion += f"<p>{resumen}</p>"

        if documento:
            descripcion += (
                f'<p><a href="{documento}">'
                f"Descargar documento de ROVI"
                f"</a></p>"
            )

        descripcion += (
            f'<p><a href="{enlace}">'
            f"Leer la noticia completa en ROVI"
            f"</a></p>"
        )

        articulos.append(
            {
                "title": titulo,
                "link": enlace,
                "guid": enlace,
                "pubDate": fecha,
                "description": descripcion,
                "author": "Laboratorios ROVI",
                "categories": [
                    "ROVI",
                    "Noticias corporativas",
                ],
                "image": imagen,
            }
        )

        enlaces_vistos.add(enlace)

    print(
        f"Noticias únicas encontradas en ROVI: "
        f"{len(articulos)}"
    )

    return articulos


def leer_articulos_anteriores():
    if not ARCHIVO_RSS.exists():
        return []

    try:
        raiz = ET.parse(ARCHIVO_RSS).getroot()
    except ET.ParseError:
        print("El RSS anterior no es válido; se reconstruirá.")
        return []

    articulos = []

    for item in raiz.findall("./channel/item"):
        categorias = [
            limpiar_texto(elemento.text)
            for elemento in item.findall("category")
            if limpiar_texto(elemento.text)
        ]

        enclosure = item.find("enclosure")
        imagen = ""

        if enclosure is not None:
            imagen = limpiar_texto(enclosure.get("url"))

        articulos.append(
            {
                "title": limpiar_texto(
                    item.findtext("title")
                ),
                "link": limpiar_texto(
                    item.findtext("link")
                ),
                "guid": limpiar_texto(
                    item.findtext("guid")
                ),
                "pubDate": limpiar_texto(
                    item.findtext("pubDate")
                ),
                "description": limpiar_texto(
                    item.findtext("description")
                ),
                "author": limpiar_texto(
                    item.findtext("author")
                ),
                "categories": categorias,
                "image": imagen,
            }
        )

    print(
        f"Noticias recuperadas del RSS anterior: "
        f"{len(articulos)}"
    )

    return articulos


def clave_articulo(articulo):
    enlace = limpiar_texto(articulo.get("link"))

    if enlace:
        return enlace.split("?")[0].rstrip("/").lower()

    return limpiar_texto(
        articulo.get("guid")
        or articulo.get("title")
    ).lower()


def fecha_ordenacion(articulo):
    try:
        fecha = parsedate_to_datetime(
            articulo["pubDate"]
        )

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)

        return fecha.timestamp()

    except (
        TypeError,
        ValueError,
        OverflowError,
        KeyError,
    ):
        return 0


def combinar_articulos(nuevos, anteriores):
    nuevos.sort(
        key=fecha_ordenacion,
        reverse=True,
    )

    resultado = []
    vistos = set()

    for articulo in nuevos + anteriores:
        clave = clave_articulo(articulo)

        if not clave or clave in vistos:
            continue

        vistos.add(clave)
        resultado.append(articulo)

        if len(resultado) >= MAX_ARTICULOS:
            break

    return resultado


def añadir_texto(padre, etiqueta, valor):
    elemento = ET.SubElement(padre, etiqueta)
    elemento.text = limpiar_texto(valor)
    return elemento


def crear_rss(articulos):
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:atom": "http://www.w3.org/2005/Atom",
        },
    )

    canal = ET.SubElement(rss, "channel")

    añadir_texto(
        canal,
        "title",
        "Laboratorios ROVI — Noticias",
    )
    añadir_texto(
        canal,
        "link",
        URL_NOTICIAS,
    )
    añadir_texto(
        canal,
        "description",
        (
            "Noticias, notas de prensa y comunicados "
            "publicados por Laboratorios ROVI."
        ),
    )
    añadir_texto(canal, "language", "es")
    añadir_texto(
        canal,
        "lastBuildDate",
        format_datetime(datetime.now(timezone.utc)),
    )
    añadir_texto(
        canal,
        "generator",
        "GitHub Actions RSS Generator",
    )

    atom = ET.SubElement(
        canal,
        "{http://www.w3.org/2005/Atom}link",
    )
    atom.set(
        "href",
        (
            "https://raw.githubusercontent.com/"
            "plis2100/rovi-noticias-rss/main/rss.xml"
        ),
    )
    atom.set("rel", "self")
    atom.set("type", "application/rss+xml")

    for articulo in articulos:
        item = ET.SubElement(canal, "item")

        añadir_texto(
            item,
            "title",
            articulo["title"],
        )
        añadir_texto(
            item,
            "link",
            articulo["link"],
        )

        guid = añadir_texto(
            item,
            "guid",
            articulo["guid"],
        )
        guid.set("isPermaLink", "true")

        añadir_texto(
            item,
            "pubDate",
            articulo["pubDate"],
        )
        añadir_texto(
            item,
            "description",
            articulo["description"],
        )
        añadir_texto(
            item,
            "author",
            articulo["author"],
        )

        for categoria in articulo["categories"]:
            añadir_texto(
                item,
                "category",
                categoria,
            )

        if articulo["image"]:
            enclosure = ET.SubElement(
                item,
                "enclosure",
            )
            enclosure.set("url", articulo["image"])
            enclosure.set("type", "image/jpeg")

    arbol = ET.ElementTree(rss)
    ET.indent(arbol, space="  ")

    temporal = ARCHIVO_RSS.with_suffix(".xml.tmp")

    arbol.write(
        temporal,
        encoding="utf-8",
        xml_declaration=True,
    )

    temporal.replace(ARCHIVO_RSS)


def main():
    if not dentro_del_horario():
        return

    nuevos = extraer_noticias()
    anteriores = leer_articulos_anteriores()

    if not nuevos and not anteriores:
        raise RuntimeError(
            "ROVI no devolvió ninguna noticia y tampoco "
            "existe un RSS anterior."
        )

    if not nuevos and anteriores:
        print(
            "AVISO: no se localizaron noticias nuevas. "
            "Se conservará el RSS anterior."
        )

    articulos = combinar_articulos(
        nuevos,
        anteriores,
    )

    if not articulos:
        raise RuntimeError(
            "No hay artículos para escribir en el RSS."
        )

    crear_rss(articulos)

    print(
        f"RSS creado correctamente con "
        f"{len(articulos)} noticias."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        raise
