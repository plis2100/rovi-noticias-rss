import html
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


URL_PORTADA = "https://www.rovi.es/es/noticias"
DOMINIO = "https://www.rovi.es"
ARCHIVO_RSS = Path("rss.xml")
MAXIMO_NOTICIAS = 1000

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.6",
    "Cache-Control": "no-cache",
}

MESES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def limpiar_texto(texto):
    if not texto:
        return ""

    return re.sub(r"\s+", " ", html.unescape(texto)).strip()


def limpiar_url(url):
    partes = urlsplit(url)
    return urlunsplit(
        (
            partes.scheme,
            partes.netloc.lower(),
            partes.path.rstrip("/"),
            "",
            "",
        )
    )


def descargar(session, url):
    ultimo_error = None

    for intento in range(1, 4):
        try:
            respuesta = session.get(
                url,
                headers=CABECERAS,
                timeout=35,
                allow_redirects=True,
            )
            respuesta.raise_for_status()
            respuesta.encoding = respuesta.apparent_encoding or "utf-8"

            print(
                f"Descargada: {url} "
                f"({len(respuesta.content)} bytes)"
            )
            return respuesta.text

        except requests.RequestException as error:
            ultimo_error = error
            print(
                f"Intento {intento}/3 fallido para {url}: {error}",
                file=sys.stderr,
            )
            time.sleep(intento * 2)

    raise RuntimeError(
        f"No se pudo descargar {url}: {ultimo_error}"
    )


def extraer_fecha(texto):
    texto = limpiar_texto(texto).lower()

    coincidencia = re.search(
        r"\b([0-3]?\d)[/\-.]([01]?\d)[/\-.]((?:19|20)\d{2})\b",
        texto,
    )

    if coincidencia:
        dia, mes, anio = map(int, coincidencia.groups())

        try:
            return datetime(
                anio,
                mes,
                dia,
                12,
                0,
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    coincidencia = re.search(
        r"\b([0-3]?\d)\s+de\s+"
        r"(enero|febrero|marzo|abril|mayo|junio|julio|"
        r"agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
        r"\s+de\s+((?:19|20)\d{2})\b",
        texto,
    )

    if coincidencia:
        dia = int(coincidencia.group(1))
        mes = MESES[coincidencia.group(2)]
        anio = int(coincidencia.group(3))

        try:
            return datetime(
                anio,
                mes,
                dia,
                12,
                0,
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    return None


def buscar_fecha_en_pagina(soup):
    selectores = [
        "meta[property='article:published_time']",
        "meta[name='date']",
        "meta[name='publication_date']",
        "meta[itemprop='datePublished']",
        "time[datetime]",
    ]

    for selector in selectores:
        elemento = soup.select_one(selector)

        if not elemento:
            continue

        valor = (
            elemento.get("content")
            or elemento.get("datetime")
            or elemento.get_text(" ", strip=True)
        )

        if not valor:
            continue

        try:
            fecha_iso = valor.strip().replace("Z", "+00:00")
            fecha = datetime.fromisoformat(fecha_iso)

            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=timezone.utc)

            return fecha.astimezone(timezone.utc)

        except (ValueError, TypeError):
            fecha = extraer_fecha(valor)

            if fecha:
                return fecha

    return extraer_fecha(soup.get_text(" ", strip=True))


def encontrar_enlaces_noticias(soup):
    encontrados = {}

    for enlace in soup.find_all("a", href=True):
        href = enlace.get("href", "").strip()
        url = limpiar_url(urljoin(URL_PORTADA, href))
        texto = limpiar_texto(enlace.get_text(" ", strip=True))

        if not url.startswith(DOMINIO):
            continue

        # Las noticias de ROVI se publican en /es/content/...
        if "/es/content/" not in url:
            continue

        slug = urlsplit(url).path.lower()

        exclusiones = (
            "/politica-",
            "/aviso-legal",
            "/terminos-",
            "/cookies",
            "/privacy",
        )

        if any(valor in slug for valor in exclusiones):
            continue

        if len(texto) < 8:
            texto = limpiar_texto(
                enlace.get("title")
                or enlace.get("aria-label")
                or ""
            )

        if url not in encontrados or len(texto) > len(encontrados[url]):
            encontrados[url] = texto

    return encontrados


def extraer_titulo(soup, titulo_portada, url):
    titulo = ""

    h1 = soup.select_one("main h1, article h1, h1")
    if h1:
        titulo = limpiar_texto(h1.get_text(" ", strip=True))

    if not titulo:
        meta = soup.select_one("meta[property='og:title']")
        if meta:
            titulo = limpiar_texto(meta.get("content", ""))

    if not titulo:
        titulo = limpiar_texto(titulo_portada)

    if not titulo:
        slug = urlsplit(url).path.rstrip("/").split("/")[-1]
        titulo = slug.replace("-", " ").capitalize()

    titulo = re.sub(
        r"\s*[|–-]\s*Rovi\s*$",
        "",
        titulo,
        flags=re.IGNORECASE,
    )

    return titulo.strip()


def extraer_descripcion(soup):
    meta = soup.select_one(
        "meta[property='og:description'], "
        "meta[name='description']"
    )

    descripcion_meta = ""
    if meta:
        descripcion_meta = limpiar_texto(meta.get("content", ""))

    contenedor = soup.select_one(
        "main article, "
        "article, "
        ".node__content, "
        ".field--name-body, "
        ".field-name-body, "
        "main"
    )

    fragmentos = []

    if contenedor:
        for etiqueta in contenedor.select(
            "script, style, nav, form, button, footer, aside"
        ):
            etiqueta.decompose()

        for elemento in contenedor.find_all(["p", "li"]):
            texto = limpiar_texto(elemento.get_text(" ", strip=True))

            if len(texto) < 35:
                continue

            if texto in fragmentos:
                continue

            fragmentos.append(texto)

            if sum(len(x) for x in fragmentos) >= 1800:
                break

    descripcion = " ".join(fragmentos)

    if len(descripcion) < 60:
        descripcion = descripcion_meta

    if len(descripcion) > 2500:
        descripcion = descripcion[:2497].rsplit(" ", 1)[0] + "..."

    return descripcion or "Noticia publicada por ROVI."


def extraer_imagen(soup, url):
    selectores = [
        "meta[property='og:image']",
        "meta[name='twitter:image']",
    ]

    for selector in selectores:
        elemento = soup.select_one(selector)

        if elemento and elemento.get("content"):
            imagen = urljoin(url, elemento["content"].strip())

            if imagen.startswith("http"):
                return imagen

    contenedor = soup.select_one(
        "main article, article, .node__content, main"
    )

    if contenedor:
        imagen = contenedor.find("img", src=True)

        if imagen:
            return urljoin(url, imagen["src"])

    return ""


def extraer_documentos(soup, url):
    documentos = []
    vistos = set()

    for enlace in soup.find_all("a", href=True):
        href = enlace["href"].strip()
        absoluta = urljoin(url, href)
        texto = limpiar_texto(enlace.get_text(" ", strip=True))

        es_documento = re.search(
            r"\.(pdf|doc|docx|xls|xlsx)(?:$|\?)",
            absoluta,
            flags=re.IGNORECASE,
        )

        es_descarga = "descargar" in texto.lower()

        if not es_documento and not es_descarga:
            continue

        if absoluta in vistos:
            continue

        vistos.add(absoluta)

        if not texto:
            texto = "Descargar documento"

        documentos.append(
            f'<a href="{html.escape(absoluta, quote=True)}">'
            f"{html.escape(texto)}</a>"
        )

    return documentos


def procesar_noticia(session, url, titulo_portada):
    try:
        contenido = descargar(session, url)
        soup = BeautifulSoup(contenido, "html.parser")

        titulo = extraer_titulo(soup, titulo_portada, url)
        fecha = buscar_fecha_en_pagina(soup)
        descripcion = extraer_descripcion(soup)
        imagen = extraer_imagen(soup, url)
        documentos = extraer_documentos(soup, url)

        if not fecha:
            print(
                f"Descartada por no encontrar fecha: {url}",
                file=sys.stderr,
            )
            return None

        descripcion_html = f"<p>{html.escape(descripcion)}</p>"

        if documentos:
            descripcion_html += (
                "<p><strong>Documentos:</strong><br>"
                + "<br>".join(documentos)
                + "</p>"
            )

        if imagen:
            descripcion_html = (
                f'<p><img src="{html.escape(imagen, quote=True)}" '
                f'alt="{html.escape(titulo, quote=True)}"></p>'
                + descripcion_html
            )

        return {
            "titulo": titulo,
            "url": limpiar_url(url),
            "fecha": fecha,
            "descripcion": descripcion_html,
            "imagen": imagen,
        }

    except Exception as error:
        print(
            f"AVISO: no se pudo procesar {url}: {error}",
            file=sys.stderr,
        )
        return None


def leer_rss_anterior():
    anteriores = {}

    if not ARCHIVO_RSS.exists():
        return anteriores

    try:
        raiz = ET.parse(ARCHIVO_RSS).getroot()
        canal = raiz.find("channel")

        if canal is None:
            return anteriores

        for item in canal.findall("item"):
            enlace = limpiar_url(item.findtext("link", "").strip())
            titulo = limpiar_texto(item.findtext("title", ""))
            descripcion = item.findtext("description", "")
            fecha_texto = item.findtext("pubDate", "")

            if not enlace or not titulo:
                continue

            try:
                from email.utils import parsedate_to_datetime

                fecha = parsedate_to_datetime(fecha_texto)

                if fecha.tzinfo is None:
                    fecha = fecha.replace(tzinfo=timezone.utc)

            except (ValueError, TypeError):
                fecha = datetime(1970, 1, 1, tzinfo=timezone.utc)

            enclosure = item.find("enclosure")
            imagen = ""

            if enclosure is not None:
                imagen = enclosure.get("url", "")

            anteriores[enlace] = {
                "titulo": titulo,
                "url": enlace,
                "fecha": fecha,
                "descripcion": descripcion,
                "imagen": imagen,
            }

    except (ET.ParseError, OSError) as error:
        print(
            f"AVISO: no se pudo leer el RSS anterior: {error}",
            file=sys.stderr,
        )

    return anteriores


def escribir_rss(noticias):
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:atom": "http://www.w3.org/2005/Atom",
        },
    )

    canal = ET.SubElement(rss, "channel")

    ET.SubElement(canal, "title").text = "Actualidad ROVI"
    ET.SubElement(canal, "link").text = URL_PORTADA
    ET.SubElement(canal, "description").text = (
        "Noticias, notas de prensa y actualidad de "
        "Laboratorios Farmacéuticos ROVI."
    )
    ET.SubElement(canal, "language").text = "es-ES"
    ET.SubElement(canal, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )
    ET.SubElement(canal, "ttl").text = "60"

    atom_link = ET.SubElement(
        canal,
        "{http://www.w3.org/2005/Atom}link",
    )
    atom_link.set(
        "href",
        "https://raw.githubusercontent.com/"
        "plis2100/rovi-noticias-rss/main/rss.xml",
    )
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    for noticia in noticias[:MAXIMO_NOTICIAS]:
        item = ET.SubElement(canal, "item")

        ET.SubElement(item, "title").text = noticia["titulo"]
        ET.SubElement(item, "link").text = noticia["url"]
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = (
            noticia["url"]
        )
        ET.SubElement(item, "pubDate").text = format_datetime(
            noticia["fecha"].astimezone(timezone.utc)
        )
        ET.SubElement(item, "description").text = noticia["descripcion"]

        if noticia.get("imagen"):
            ET.SubElement(
                item,
                "enclosure",
                {
                    "url": noticia["imagen"],
                    "type": "image/jpeg",
                },
            )

    ET.indent(rss, space="  ")

    arbol = ET.ElementTree(rss)
    arbol.write(
        ARCHIVO_RSS,
        encoding="utf-8",
        xml_declaration=True,
    )


def main():
    session = requests.Session()

    pagina = descargar(session, URL_PORTADA)
    soup = BeautifulSoup(pagina, "html.parser")

    enlaces = encontrar_enlaces_noticias(soup)

    print(f"Enlaces de noticias encontrados en ROVI: {len(enlaces)}")

    nuevas = {}

    for numero, (url, titulo_portada) in enumerate(
        enlaces.items(),
        start=1,
    ):
        print(f"Procesando {numero}/{len(enlaces)}: {url}")

        noticia = procesar_noticia(
            session,
            url,
            titulo_portada,
        )

        if noticia:
            nuevas[noticia["url"]] = noticia

        time.sleep(0.25)

    anteriores = leer_rss_anterior()

    # Las noticias nuevas sustituyen a versiones anteriores.
    todas = dict(anteriores)
    todas.update(nuevas)

    noticias_ordenadas = sorted(
        todas.values(),
        key=lambda noticia: noticia["fecha"],
        reverse=True,
    )

    print(f"Noticias recuperadas ahora: {len(nuevas)}")
    print(f"Noticias conservadas del RSS anterior: {len(anteriores)}")
    print(f"Total de noticias en el RSS: {len(noticias_ordenadas)}")

    if not noticias_ordenadas:
        raise RuntimeError(
            "ROVI no devolvió noticias y tampoco existe "
            "un RSS anterior."
        )

    escribir_rss(noticias_ordenadas)

    print("RSS de ROVI generado correctamente.")


if __name__ == "__main__":
    main()
