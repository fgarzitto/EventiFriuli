import os
import time
import re
import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import cloudscraper
import gspread
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials


# ================= CONFIG =================

URL_BASE = "https://www.itinerarinellarte.it"
URL_EVENTI = f"{URL_BASE}/it/mostre/friuli-venezia-giulia"

# Con 7 vengono considerati oggi e i successivi 7 giorni.
GIORNI_AVANTI = 7

# Numero massimo di pagine successive da controllare.
# Con 4 vengono controllate la prima pagina più altre quattro.
MAX_PAGES = 4

RISULTATI_PER_PAGINA = 10
SLEEP_TIME = 2

SHEET_NAME = "Eventi in Friuli"
WORKSHEET_NAME = "Itinerarinellarte"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

MESI = {
    1: "Gen",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "Mag",
    6: "Giu",
    7: "Lug",
    8: "Ago",
    9: "Set",
    10: "Ott",
    11: "Nov",
    12: "Dic"
}

DATE_RE = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{4})\b"
)

EVENT_PATH_RE = re.compile(
    r"^/it/mostre/.+-\d+/?$"
)


# ================= UTILS =================

def parse_data(text):
    try:
        return datetime.strptime(
            text.strip(),
            "%d/%m/%Y"
        ).date()

    except (TypeError, ValueError):
        return None


def normalizza_spazi(text):
    return " ".join(
        (text or "").split()
    )


def is_event_link(href):
    if not href:
        return False

    url_completo = urljoin(
        URL_BASE,
        href
    )

    path = urlparse(
        url_completo
    ).path

    return bool(
        EVENT_PATH_RE.match(path)
    )


def trova_contenitore_evento(link_elem):
    """
    Cerca il contenitore HTML più vicino che racchiude
    titolo, date e luogo dell'evento.
    """

    parent = link_elem

    for _ in range(8):
        parent = parent.parent

        if parent is None:
            return None

        testo = normalizza_spazi(
            parent.get_text(
                " ",
                strip=True
            )
        )

        date_trovate = DATE_RE.findall(
            testo
        )

        if len(date_trovate) < 2:
            continue

        link_evento_nel_blocco = [
            link
            for link in parent.find_all(
                "a",
                href=True
            )
            if is_event_link(
                link.get("href")
            )
        ]

        # Una scheda può avere più link allo stesso evento,
        # per esempio uno sull'immagine e uno sul titolo.
        if len(link_evento_nel_blocco) <= 3:
            return parent

    return None


def estrai_luogo(container):
    stringhe = [
        normalizza_spazi(stringa)
        for stringa in container.stripped_strings
    ]

    # Cerca una stringa simile a:
    # "Friuli Venezia Giulia, Udine"
    for testo in reversed(stringhe):
        if "Friuli Venezia Giulia" not in testo:
            continue

        if "," not in testo:
            continue

        luogo = testo.split(
            ",",
            1
        )[1]

        luogo = re.sub(
            r"\bevento concluso\b",
            "",
            luogo,
            flags=re.IGNORECASE
        )

        luogo = normalizza_spazi(
            luogo
        ).strip(" -")

        if luogo:
            return luogo

    # Ricerca alternativa nell'intero testo della scheda.
    testo_completo = normalizza_spazi(
        container.get_text(
            " ",
            strip=True
        )
    )

    match = re.search(
        r"Friuli Venezia Giulia\s*,\s*(.+?)"
        r"(?:\s+evento concluso|$)",
        testo_completo,
        flags=re.IGNORECASE
    )

    if match:
        luogo = normalizza_spazi(
            match.group(1)
        ).strip(" -")

        if luogo:
            return luogo

    return "Luogo non disponibile"


# ================= ESTRAZIONE EVENTI =================

def estrai_eventi(soup):
    eventi = []

    # Si confrontano solo le date, senza ore e minuti.
    oggi = datetime.now().date()

    limite = oggi + timedelta(
        days=GIORNI_AVANTI
    )

    link_candidati = [
        link
        for link in soup.find_all(
            "a",
            href=True
        )
        if is_event_link(
            link.get("href")
        )
    ]

    logging.info(
        "Link evento candidati nella pagina: %s",
        len(link_candidati)
    )

    link_gia_letti = set()
    schede_lette = 0

    for link_elem in link_candidati:
        titolo = normalizza_spazi(
            link_elem.get_text(
                " ",
                strip=True
            )
        )

        # Il link sull'immagine potrebbe non contenere testo.
        if not titolo:
            continue

        href = urljoin(
            URL_BASE,
            link_elem.get("href")
        )

        href = href.split(
            "#",
            1
        )[0]

        if href in link_gia_letti:
            continue

        container = trova_contenitore_evento(
            link_elem
        )

        if container is None:
            logging.warning(
                "Contenitore non trovato per: %s",
                titolo
            )
            continue

        testo_container = normalizza_spazi(
            container.get_text(
                " ",
                strip=True
            )
        )

        date_trovate = DATE_RE.findall(
            testo_container
        )

        if not date_trovate:
            continue

        data_inizio = parse_data(
            date_trovate[0]
        )

        if len(date_trovate) >= 2:
            data_fine = parse_data(
                date_trovate[1]
            )
        else:
            data_fine = data_inizio

        if not data_inizio or not data_fine:
            continue

        if data_fine < data_inizio:
            logging.warning(
                "Intervallo date non valido per '%s': %s - %s",
                titolo,
                data_inizio,
                data_fine
            )
            continue

        link_gia_letti.add(
            href
        )

        schede_lette += 1

        # Esclude gli eventi già terminati.
        if data_fine < oggi:
            continue

        # Esclude gli eventi che iniziano oltre il periodo richiesto.
        if data_inizio > limite:
            continue

        primo_giorno = max(
            data_inizio,
            oggi
        )

        ultimo_giorno = min(
            data_fine,
            limite
        )

        luogo = estrai_luogo(
            container
        )

        numero_giorni = (
            ultimo_giorno - primo_giorno
        ).days

        # Crea una riga per ogni giorno in cui la mostra è attiva.
        for i in range(numero_giorni + 1):
            giorno = primo_giorno + timedelta(
                days=i
            )

            eventi.append({
                "titolo": titolo,
                "data": (
                    f"{giorno.day:02d} "
                    f"{MESI[giorno.month]} "
                    f"{giorno.year}"
                ),
                "data_sort": giorno,
                "ora": "Ora non disponibile",
                "luogo": luogo,
                "link": href,
                "categoria": "Mostre"
            })

    logging.info(
        "Schede evento interpretate: %s",
        schede_lette
    )

    logging.info(
        "Righe nella finestra temporale: %s",
        len(eventi)
    )

    return eventi, schede_lette


# ================= SCRAPING =================

def crea_scraper():
    return cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "desktop": True
        }
    )


def scarica_eventi():
    scraper = crea_scraper()

    eventi_totali = []
    chiavi_eventi = set()

    headers = {
        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Accept-Language": (
            "it-IT,it;q=0.9,en;q=0.8"
        ),
        "Cache-Control": "no-cache"
    }

    for page in range(MAX_PAGES + 1):
        offset = page * RISULTATI_PER_PAGINA

        if offset == 0:
            url = URL_EVENTI
        else:
            url = (
                f"{URL_EVENTI}"
                f"?eventi_pg_from={offset}"
            )

        logging.info(
            "Scraping pagina %s: %s",
            page + 1,
            url
        )

        try:
            response = scraper.get(
                url,
                headers=headers,
                timeout=20
            )

            response.raise_for_status()

        except Exception as exc:
            logging.error(
                "Errore richiesta pagina %s: %s",
                page + 1,
                exc
            )
            continue

        logging.info(
            "Risposta pagina %s: "
            "status=%s, bytes=%s, url_finale=%s",
            page + 1,
            response.status_code,
            len(response.content),
            response.url
        )

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        if soup.title:
            titolo_pagina = soup.title.get_text(
                " ",
                strip=True
            )
        else:
            titolo_pagina = "Senza titolo"

        logging.info(
            "Titolo HTML: %s",
            titolo_pagina
        )

        eventi, schede_lette = estrai_eventi(
            soup
        )

        # Se non viene riconosciuta nessuna scheda,
        # salva l'HTML ricevuto per consentire il controllo.
        if schede_lette == 0:
            debug_file = (
                "debug_itinerarinellarte_"
                f"pagina_{page + 1}.html"
            )

            with open(
                debug_file,
                "w",
                encoding="utf-8"
            ) as file:
                file.write(
                    response.text
                )

            logging.error(
                "Nessuna scheda riconosciuta. "
                "HTML salvato in %s",
                debug_file
            )

            break

        for evento in eventi:
            chiave = (
                evento["link"],
                evento["data_sort"]
            )

            if chiave in chiavi_eventi:
                continue

            chiavi_eventi.add(
                chiave
            )

            eventi_totali.append(
                evento
            )

        time.sleep(
            SLEEP_TIME
        )

    return eventi_totali


# ================= GOOGLE SHEETS =================

def apri_worksheet():
    # Sono richiesti soltanto i due secret
    # già utilizzati dal vecchio script.
    variabili_obbligatorie = [
        "GSHEET_PRIVATE_KEY",
        "GSHEET_CLIENT_EMAIL"
    ]

    mancanti = [
        nome
        for nome in variabili_obbligatorie
        if not os.getenv(nome)
    ]

    if mancanti:
        raise RuntimeError(
            "Variabili d'ambiente mancanti: "
            + ", ".join(mancanti)
        )

    private_key = os.getenv(
        "GSHEET_PRIVATE_KEY"
    ).replace(
        "\\n",
        "\n"
    )

    client_email = os.getenv(
        "GSHEET_CLIENT_EMAIL"
    )

    credentials_info = {
        "type": "service_account",
        "project_id": "EventiFriuli",
        "private_key_id": os.getenv(
            "GSHEET_PRIVATE_KEY_ID"
        ),
        "private_key": private_key,
        "client_email": client_email,
        "client_id": os.getenv(
            "GSHEET_CLIENT_ID"
        ),
        "auth_uri": (
            "https://accounts.google.com/"
            "o/oauth2/auth"
        ),
        "token_uri": (
            "https://oauth2.googleapis.com/token"
        ),
        "auth_provider_x509_cert_url": (
            "https://www.googleapis.com/"
            "oauth2/v1/certs"
        ),
        "client_x509_cert_url": (
            "https://www.googleapis.com/"
            "robot/v1/metadata/x509/"
            + client_email
        )
    }

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    credentials = (
        ServiceAccountCredentials
        .from_json_keyfile_dict(
            credentials_info,
            scope
        )
    )

    client = gspread.authorize(
        credentials
    )

    spreadsheet = client.open(
        SHEET_NAME
    )

    return spreadsheet.worksheet(
        WORKSHEET_NAME
    )


# ================= MAIN =================

def main():
    # Lo scraping viene eseguito prima di modificare il foglio.
    eventi_totali = scarica_eventi()

    if not eventi_totali:
        logging.error(
            "Nessun evento trovato. "
            "Il foglio Google non è stato modificato."
        )
        return

    eventi_totali.sort(
        key=lambda evento: (
            evento["data_sort"],
            evento["titolo"].casefold(),
            evento["luogo"].casefold()
        )
    )

    righe = [
        [
            evento["titolo"],
            evento["data"],
            evento["ora"],
            evento["luogo"],
            evento["link"],
            evento["categoria"]
        ]
        for evento in eventi_totali
    ]

    sheet = apri_worksheet()

    logging.info(
        "Accesso a Google Sheets riuscito"
    )

    # Cancella soltanto i contenuti dalla seconda riga in poi,
    # conservando le intestazioni della prima riga.
    sheet.batch_clear([
        "A2:F"
    ])

    sheet.append_rows(
        righe,
        value_input_option="USER_ENTERED"
    )

    logging.info(
        "%s righe caricate su Google Sheets",
        len(righe)
    )


# ================= START =================

if __name__ == "__main__":
    main()
