"""
Bot Telegram de suivi de vols pour pilotes.
- Lit Google Calendar du pilote
- Notifie les proches (abonnés) des décollages / atterrissages
- Gère aviation générale (TDP / tours de piste)

Modifications de cette version (oct. 2026) :
  1. Noms d'aéroports affichés (table extensible, repli sur code IATA seul)
  2. Détection des immatriculations d'avion dans titre/description/lieu
  3. Commande /stop (alias /desabonner) pour qu'un proche se désabonne
  4. Normalisation BOT_USERNAME + récupération via bot.getMe()
  5. Aviation générale : séances multi-TDP (inhibition notifs intermédiaires)
  6. Correction bug notifs de vols futurs + rattrapage au démarrage
"""

# === STARTUP BANNER + LOGGING (AVANT les imports lourds) ====================
# Sur Render, stdout est un pipe non-TTY : Python peut bufferiser les prints
# jusqu'à plusieurs ko. On force le line-buffering ET on configure le logging
# standard sur sys.stdout AVANT d'importer flask/google libs, pour que toutes
# les bibliothèques (google.auth, urllib3, werkzeug…) écrivent aussi
# directement dans la console Render.
import sys
import os
print(
    "[bot_vol] Démarrage du processus bot_vol.py (PID={}, Python={})".format(
        os.getpid(), sys.version.split()[0]
    ),
    file=sys.stdout,
    flush=True,
)
# Force line-buffering sur stdout/stderr (équivalent à PYTHONUNBUFFERED=1).
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass
# Configure logging : envoie tout sur sys.stdout avec flush immédiat.
import logging
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    force=True,
)
# Réduire le bruit des libs très verbeuses (urllib3, google) au démarrage.
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("google.auth").setLevel(logging.WARNING)
print(
    "[bot_vol] logging.basicConfig OK (stream=sys.stdout, level=INFO)",
    flush=True,
)

# === Imports lourds ========================================================
import re
import json
import time
import sqlite3
import random
import string
import datetime
import threading
import requests
from flask import Flask, request, redirect, url_for
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

print("[bot_vol] Imports lourds OK (flask + google-api-client)", flush=True)

# --- CONFIGURATION SÉCURISÉE (variables d'env) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLIENT_SECRET_JSON = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")

# BOT_USERNAME : nom d'utilisateur public du bot (sans @). Sera normalisé.
# Si absent/invalide au démarrage, on tentera de le récupérer via getMe().
_raw_bot_username = os.environ.get("BOT_USERNAME", "")
BOT_USERNAME = ""  # valeur normalisée, remplie par _resolve_bot_username()

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

# --- CONSTANTES DE DÉTECTION DE VOL ---
# Codes exclus : faux positifs courants (mots anglais/français qui matchent
# un préfixe 2 lettres + chiffres).
CODES_IATA_EXCLUS = {
    'OFF', 'DAY', 'SIM', 'STB', 'REC', 'LAT', 'VOL', 'REST', 'RDO',
    'DUT', 'DUTY', 'TRAIN', 'TRNG', 'FORM', 'MCC', 'LIF', 'LEAVE',
    'CONG', 'SICK', 'VAC', 'BRF', 'BRIE', 'MEET', 'REP', 'POS',
    'HOTEL', 'HOME', 'BASE', 'CREW', 'FLT', 'FDP', 'EFB', 'TBD',
}

# Codes ICAO 3 lettres de compagnies connus (transport régulier).
CODES_ICAO_COMPAGNIES_BASE = {
    'AFR', 'AIC', 'AUA', 'BAW', 'BER', 'CFG', 'CLH', 'DLH', 'EZY',
    'FIN', 'KLM', 'QFA', 'QTR', 'RYR', 'SAS', 'SWR', 'THY', 'UAE',
    'VIR', 'WZZ',
}

# Préfixes supplémentaires demandés (cargo / fret).
EXTRA_AIRLINE_CODES = {'FDX', 'TAY', 'UPS', '3V'}  # 3V = cargolux ITA (TAREX)

CODES_ICAO_COMPAGNIES = CODES_ICAO_COMPAGNIES_BASE | EXTRA_AIRLINE_CODES

# Code IATA compagnie : exactement 2 caractères alphanum. dont au moins
# une lettre (donc accepte 3V, mais rejette "12"). Bornes word-boundary
# empêchent de capturer un sous-code dans TXB235 / ABC123.
CODE_IATA_VOL = (
    r'(?=[A-Z0-9]{2}(?:[- ]?\d{1,4})(?![A-Z0-9]))'
    r'(?=[A-Z0-9]*[A-Z])'
    r'[A-Z0-9]{2}'
)

REGEX_NUMERO_VOL = re.compile(
    r'(?<![A-Z0-9])((?:'
    + '|'.join(sorted(CODES_ICAO_COMPAGNIES, key=len, reverse=True))
    + '|'
    + CODE_IATA_VOL
    + r'))[- ]?(\d{1,4})(?![A-Z0-9])',
    re.IGNORECASE,
)

REGEX_CODE_IATA = re.compile(r'(?<![A-Z])([A-Z]{3})(?![A-Z])', re.IGNORECASE)
# Codes ICAO 4 lettres (LFMD, EGLL...) : utilisés pour la détection AG
# (boucle type LFXX-LFXX). Pas utilisé pour la règle principale "2 IATA".
REGEX_CODE_ICAO = re.compile(r'(?<![A-Z])([A-Z]{4})(?![A-Z])', re.IGNORECASE)

# --- DÉTECTION IMMATRICULATIONS D'AVION ---
# Formats courants : F-XXXX (France), OO-XXX (Belgique), D-XXXX (Allemagne),
# G-XXXX (Royaume-Uni), HB-XXX (Suisse), N12345 (USA), EC-XXX (Espagne),
# I-XXXX (Italie), CS-XXX (Portugal), SE-XXX (Suède), etc.
# Regex stricte avec look-arounds : on veut un "mot" complet.
REGEX_IMMATRICULATION = re.compile(
    r'(?<![A-Z0-9])('
    r'(?:F|OO|D|G|HB|N|EC|I|CS|SE|EI|LN|OY|OH|OK|YL|HA|JA|HS|VH|'
    r'ZK|PT|TC|TF|SP|YA|A6|A7|A9|HZ)'
    r')-?[A-Z0-9]{2,5}(?![A-Z0-9])',
    re.IGNORECASE,
)

# --- CONSTANTES AVIATION GÉNÉRALE ---
# Durée moyenne d'un tour de piste (TDP) en minutes.
TDP_MINUTES = int(os.environ.get("TDP_MINUTES", "10"))
# Fenêtre max (en heures) après l'heure de début pour notifier un décollage.
FENETRE_DECOLLAGE_HEURES = float(os.environ.get("FENETRE_DECOLLAGE_HEURES", "6"))
# Seuil durée totale (en minutes) pour considérer un événement comme AG.
SEUIL_AG_MINUTES = int(os.environ.get("SEUIL_AG_MINUTES", "90"))

# --- TABLE INTÉGRÉE DES NOMS D'AÉROPORTS (modifiable) ---
# Table par défaut (extensible via la variable d'env AIRPORT_NAMES_JSON).
AIRPORT_NAMES = {
    # France
    'CDG': 'Paris-Charles de Gaulle',
    'ORY': 'Paris-Orly',
    'LYS': 'Lyon-Saint-Exupéry',
    'MRS': 'Marseille-Provence',
    'NCE': 'Nice-Côte d\u2019Azur',
    'TLS': 'Toulouse-Blagnac',
    'BOD': 'Bordeaux-Mérignac',
    'NTE': 'Nantes-Atlantique',
    'BSL': 'Bâle-Mulhouse',
    'MLH': 'Bâle-Mulhouse',
    'LBG': 'Paris-Le Bourget',
    'LDE': 'Tarbes-Lourdes-Pyrénées',
    'LFK': 'Figari-Sud Corse',
    'PGF': 'Perpignan-Rivesaltes',
    'BIQ': 'Biarritz-Pays Basque',
    'FSC': 'Figari-Sud Corse',
    'AJA': 'Ajaccio-Napoléon-Bonaparte',
    'CLY': 'Calvi-Sainte-Catherine',
    'BIA': 'Bastia-Poretta',
    'RNS': 'Rennes-Bretagne',
    'UIP': 'Quimper-Bretagne',
    'CFR': 'Caen-Carpiquet',
    'LEH': 'Le Havre-Octeville',
    'ETZ': 'Metz-Nancy-Lorraine',
    'LME': 'Le Mans-Arnage',
    'TUF': 'Tours-Val de Loire',
    'CFE': 'Clermont-Ferrand-Auvergne',
    'EGC': 'Bergerac-Dordogne-Périgord',
    'AGF': 'Agen-La Garenne',
    'LIG': 'Limoges-Bellegarde',
    'BVE': 'Brive-Souillac',
    'RDZ': 'Rodez-Marcillac',
    'LRT': 'Lorient-Bretagne Sud',
    'SBK': 'Saint-Brieuc-Armor',
    'DNR': 'Dinard-Pleurtuit-Saint-Malo',
    # Belgique / Pays-Bas / Luxembourg
    'BRU': 'Bruxelles-National',
    'CRL': 'Bruxelles-Charleroi',
    'AMS': 'Amsterdam-Schiphol',
    'RTM': 'Rotterdam-La Haye',
    'EIN': 'Eindhoven',
    'LUX': 'Luxembourg-Findel',
    # Allemagne
    'FRA': 'Francfort-sur-le-Main',
    'MUC': 'Munich-Franz-Josef Strauss',
    'BER': 'Berlin-Brandebourg',
    'TXL': 'Berlin-Tegel',
    'CGN': 'Cologne-Bonn',
    'DUS': 'Düsseldorf',
    'HAM': 'Hambourg',
    'HAJ': 'Hanovre-Langenhagen',
    'LEJ': 'Leipzig/Halle',
    'STR': 'Stuttgart',
    'NUE': 'Nuremberg',
    'BRE': 'Brême',
    'DTM': 'Dortmund',
    # Royaume-Uni / Irlande
    'LHR': 'Londres-Heathrow',
    'LGW': 'Londres-Gatwick',
    'STN': 'Londres-Stansted',
    'LTN': 'Londres-Luton',
    'MAN': 'Manchester',
    'BHX': 'Birmingham',
    'EDI': 'Édimbourg',
    'GLA': 'Glasgow',
    'DUB': 'Dublin',
    # Suisse / Autriche / Italie / Espagne / Portugal
    'ZRH': 'Zurich-Kloten',
    'GVA': 'Genève-Cointrin',
    'VIE': 'Vienne-Schwechat',
    'INN': 'Innsbruck',
    'FCO': 'Rome-Fiumicino',
    'CIA': 'Rome-Ciampino',
    'MXP': 'Milan-Malpensa',
    'LIN': 'Milan-Linate',
    'VCE': 'Venise-Marco Polo',
    'NAP': 'Naples-Capodichino',
    'BLQ': 'Bologne-Marconi',
    'MAD': 'Madrid-Barajas',
    'BCN': 'Barcelone-El Prat',
    'VLC': 'Valence-Manises',
    'AGP': 'Málaga-Costa del Sol',
    'SVQ': 'Séville-San Pablo',
    'LIS': 'Lisbonne-Humberto Delgado',
    'OPO': 'Porto-Francisco Sá Carneiro',
    # Scandinavie
    'CPH': 'Copenhague-Kastrup',
    'OSL': 'Oslo-Gardermoen',
    'ARN': 'Stockholm-Arlanda',
    'HEL': 'Helsinki-Vantaa',
    # Europe de l'Est / divers
    'PRG': 'Prague-Václav Havel',
    'WAW': 'Varsovie-Chopin',
    'BUD': 'Budapest-Ferenc Liszt',
    'OTP': 'Bucarest-Henri-Coandă',
    'SOF': 'Sofia-Vrazhdebna',
    'ATH': 'Athènes-Elefthérios-Venizélos',
    # Moyen-Orient / Asie
    'DXB': 'Dubaï-International',
    'AUH': 'Abou Dabi',
    'DOH': 'Doha-Hamad',
    'IST': 'Istanbul',
    'TLV': 'Tel Aviv-Ben Gourion',
    'HND': 'Tokyo-Haneda',
    'NRT': 'Tokyo-Narita',
    'ICN': 'Séoul-Incheon',
    'PEK': 'Pékin-Capitale',
    'PVG': 'Shanghai-Pudong',
    'HKG': 'Hong Kong-Chek Lap Kok',
    'SIN': 'Singapour-Changi',
    'BKK': 'Bangkok-Suvarnabhumi',
    'KUL': 'Kuala Lumpur',
    'DEL': 'Delhi-Indira Gandhi',
    'BOM': 'Mumbai-Chhatrapati Shivaji',
    # Afrique
    'CMN': 'Casablanca-Mohammed V',
    'RAK': 'Marrakech-Ménara',
    'TUN': 'Tunis-Carthage',
    'ALG': 'Alger-Houari Boumédiène',
    'CAI': 'Le Caire',
    'JNB': 'Johannesburg-OR Tambo',
    'NBO': 'Nairobi-Jomo Kenyatta',
    'DKR': 'Dakar-Blaise Diagne',
    # Amériques
    'JFK': 'New York-JFK',
    'EWR': 'Newark-Liberty',
    'LGA': 'New York-LaGuardia',
    'ATL': 'Atlanta-Hartsfield-Jackson',
    'ORD': 'Chicago-O\u2019Hare',
    'DFW': 'Dallas/Fort Worth',
    'DEN': 'Denver-International',
    'LAX': 'Los Angeles-International',
    'SFO': 'San Francisco-International',
    'SEA': 'Seattle-Tacoma',
    'MIA': 'Miami-International',
    'BOS': 'Boston-Logan',
    'YYZ': 'Toronto-Pearson',
    'YVR': 'Vancouver-International',
    'MEX': 'Mexico-Benito Juárez',
    'GRU': 'São Paulo-Guarulhos',
    'EZE': 'Buenos Aires-Ezeiza',
    'SCL': 'Santiago du Chili',
    # Fret / hubs cargo
    'MEM': 'Memphis-FedEx Superhub',
    'IND': 'Indianapolis-FedEx Hub',
    'SDF': 'Louisville-UPS Worldport',
    'ANC': 'Anchorage-Ted Stevens',
    'LGG': 'Liège-Bierset',
    'VKO': 'Moscou-Vnoukovo',
}

# Surcharge optionnelle par variable d'env AIRPORT_NAMES_JSON.
_airport_override = os.environ.get("AIRPORT_NAMES_JSON", "").strip()
if _airport_override:
    try:
        AIRPORT_NAMES.update(json.loads(_airport_override))
    except Exception as _e:  # noqa
        print(f"[bot_vol] AIRPORT_NAMES_JSON invalide : {_e}")

# Initialisation de Flask (Serveur Web unique)
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-rafael")

# ---------------------------------------------------------------------------
# BASE DE DONNÉES SQLITE
# ---------------------------------------------------------------------------
DB_FILE = "bot_vols.db"

def init_db():
    """Crée les tables si absentes et ajoute une table dedup."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS pilotes (
            chat_id INTEGER PRIMARY KEY,
            nom TEXT,
            code_invitation TEXT UNIQUE,
            google_token TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS proches (
            chat_id INTEGER PRIMARY KEY,
            pilote_chat_id INTEGER,
            FOREIGN KEY (pilote_chat_id) REFERENCES pilotes (chat_id)
        )
    ''')
    # Table de dédoublonnage persistant des notifications envoyées.
    c.execute('''
        CREATE TABLE IF NOT EXISTS notifs_envoyees (
            chat_id INTEGER,
            event_id TEXT,
            type TEXT,
            sent_at TEXT,
            PRIMARY KEY (chat_id, event_id, type)
        )
    ''')
    conn.commit()
    conn.close()

init_db()


def generer_code_invitation():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


# ---------------------------------------------------------------------------
# NORMALISATION BOT_USERNAME
# ---------------------------------------------------------------------------
def _normalize_bot_username(value):
    """Nettoie BOT_USERNAME : enlève @, espaces, URL https://t.me/, etc."""
    if not value:
        return ""
    v = str(value).strip()
    # Retire les préfixes d'URL / @ les plus courants.
    lowered = v.lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
        if lowered.startswith(prefix):
            v = v[len(prefix):]
            break
    # Retire tout slash / espace / @ restants.
    v = v.replace("/", "").replace(" ", "").lstrip("@")
    # Doit commencer par une lettre, ne contenir que [A-Za-z0-9_], 5-32 chars.
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", v):
        return ""
    return v


def _resolve_bot_username():
    """Récupère le vrai username du bot via l'API Telegram (getMe)."""
    global BOT_USERNAME
    BOT_USERNAME = _normalize_bot_username(_raw_bot_username)
    if BOT_USERNAME:
        return BOT_USERNAME
    # Sinon, on tente getMe() une fois pour récupérer le username réel.
    if TELEGRAM_TOKEN:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe",
                timeout=10,
            )
            data = r.json()
            if data.get("ok"):
                BOT_USERNAME = _normalize_bot_username(
                    data.get("result", {}).get("username", "")
                )
                if BOT_USERNAME:
                    print(f"[bot_vol] BOT_USERNAME récupéré via getMe : {BOT_USERNAME}")
        except Exception as e:
            print(f"[bot_vol] getMe a échoué : {e}")
    return BOT_USERNAME


# ---------------------------------------------------------------------------
# ROUTES FLASK (OAUTH2 & SANTE)
# ---------------------------------------------------------------------------
@app.route('/')
def home():
    return "🤖 Bot Telegram & OAuth actif !"

@app.route('/login')
def login():
    chat_id = request.args.get('chat_id')
    if not chat_id or not CLIENT_SECRET_JSON:
        return "Erreur : Paramètres d'authentification manquants.", 400

    client_config = json.loads(CLIENT_SECRET_JSON)
    redirect_uri = f"{RENDER_EXTERNAL_URL}/oauth2callback"

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

    auth_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        state=chat_id
    )
    return redirect(auth_url)

@app.route('/oauth2callback')
def oauth2callback():
    chat_id = request.args.get('state')
    code = request.args.get('code')

    if not chat_id or not code or not CLIENT_SECRET_JSON:
        return "Échec de l'authentification.", 400

    try:
        client_config = json.loads(CLIENT_SECRET_JSON)
        redirect_uri = f"{RENDER_EXTERNAL_URL}/oauth2callback"

        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )
        flow.fetch_token(code=code)
        credentials = flow.credentials
        token_data = credentials.to_json()

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT code_invitation FROM pilotes WHERE chat_id = ?', (chat_id,))
        row = c.fetchone()
        code_inv = row[0] if row else generer_code_invitation()

        c.execute('''
            INSERT INTO pilotes (chat_id, code_invitation, google_token)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET google_token = excluded.google_token
        ''', (chat_id, code_inv, token_data))
        conn.commit()
        conn.close()

        # Construit un lien d'invitation propre (https://t.me/USERNAME?start=CODE).
        invite_url = _build_invite_link(code_inv)
        if invite_url:
            link_msg = (
                f"Partagez ce lien à vos proches :\n{invite_url}\n"
                f"(ou donnez-leur le code `{code_inv}` à utiliser avec `/join {code_inv}`)"
            )
        else:
            link_msg = (
                f"Partagez-leur le code `{code_inv}` pour qu'ils s'abonnent avec "
                f"`/join {code_inv}`."
            )

        envoyer_message_telegram(
            chat_id,
            f"✅ **Compte Google connecté avec succès !**\n\n"
            f"Votre code d'invitation pour vos proches est : `{code_inv}`\n\n"
            f"{link_msg}"
        )

        return "<h1>Connexion réussie !</h1><p>Vous pouvez fermer cette fenêtre et retourner sur Telegram.</p>"
    except Exception as e:
        return f"Erreur lors de la validation OAuth : {e}", 500


# ---------------------------------------------------------------------------
# LOGIQUE BOT TELEGRAM
# ---------------------------------------------------------------------------
def envoyer_message_telegram(chat_id, texte):
    """Envoie un message Telegram (Markdown)."""
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texte,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Erreur envoi Telegram : {e}")


def envoyer_boutons_telegram(chat_id, texte, boutons):
    """Envoie un message avec un clavier inline (boutons)."""
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texte,
        "parse_mode": "Markdown",
        "reply_markup": json.dumps({"inline_keyboard": boutons}),
        "disable_web_page_preview": False,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Erreur envoi Telegram : {e}")


def _build_invite_link(code):
    """Construit https://t.me/USERNAME?start=CODE en utilisant BOT_USERNAME."""
    if not BOT_USERNAME:
        return None
    return f"https://t.me/{BOT_USERNAME}?start={code}"


# --- Noms d'aéroports -----------------------------------------------------
def format_iata(code):
    """Renvoie 'Ville-Nom (CODE)' si connu, sinon 'CODE'."""
    if not code:
        return ""
    code_u = code.upper().strip()
    name = AIRPORT_NAMES.get(code_u)
    return f"{name} ({code_u})" if name else code_u


# --- Détection numéro de vol / codes IATA --------------------------------
def texte_evenement(event):
    """Concatène summary/description/location pour les regex."""
    parts = []
    for key in ("summary", "description", "location"):
        val = event.get(key, "") or ""
        parts.append(str(val))
    return "\n".join(parts)


def extraire_numero_vol(titre_event):
    """Détecte un numéro de vol valide (IATA/ICAO + chiffres)."""
    for m in REGEX_NUMERO_VOL.finditer(titre_event or ""):
        code = m.group(1).upper()
        if code in CODES_IATA_EXCLUS:
            continue
        if len(code) == 2 or code in CODES_ICAO_COMPAGNIES:
            return f"{code}{m.group(2)}"
    return None


def extraire_codes_iata(text):
    """Renvoie la liste triée des codes IATA (3 lettres) trouvés."""
    return sorted({
        m.group(1).upper()
        for m in REGEX_CODE_IATA.finditer(text or "")
        if m.group(1).upper() not in CODES_IATA_EXCLUS
    })


# --- Détection immatriculations -------------------------------------------
def extraire_immatriculations(event):
    """Cherche les immatriculations d'avion dans titre/description/lieu."""
    txt = texte_evenement(event)
    return sorted({
        m.group(0).upper().replace(" ", "")
        for m in REGEX_IMMATRICULATION.finditer(txt)
    })


# --- Aviation générale (TDP) ----------------------------------------------
def _parse_dt(value):
    """Parse une date Google Calendar (UTC aware). Retourne datetime UTC
    aware, ou None. Gère 'dateTime' (Z/+offset) et 'date' (toute la journée)."""
    if not value:
        return None
    s = value.get('dateTime') or value.get('date')
    if not s:
        return None
    try:
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # date 'all-day' → on suppose UTC.
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def duree_evenement_minutes(event):
    """Durée de l'événement en minutes (None si non calculable)."""
    start = _parse_dt(event.get('start', {}))
    end = _parse_dt(event.get('end', {}))
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds() / 60))


def est_evenement_ag(event):
    """Heuristique : aviation générale = même code départ/arrivée (IATA 3L
    ou ICAO 4L type LFXX-LFXX) ou très courte durée (<= SEUIL_AG_MINUTES)."""
    txt = texte_evenement(event)
    iata = extraire_codes_iata(txt)
    if len(iata) >= 2 and iata[0] == iata[1]:
        return True
    # Boucle type LFXX-LFXX (4 lettres ICAO aviation générale).
    icao = [m.upper() for m in REGEX_CODE_ICAO.findall(txt)
            if m.upper() not in {'FLIGHT', 'DUTY', 'STAND', 'VOILE'}]
    if len(icao) >= 2 and icao[0] == icao[1]:
        return True
    duree = duree_evenement_minutes(event)
    if duree is not None and duree <= SEUIL_AG_MINUTES:
        return True
    return False


# --- Dédoublonnage persistant ---------------------------------------------
def notif_deja_envoyee(chat_id, event_id, typ):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT 1 FROM notifs_envoyees WHERE chat_id=? AND event_id=? AND type=?',
        (chat_id, event_id, typ),
    )
    row = c.fetchone()
    conn.close()
    return row is not None


def marquer_notif(chat_id, event_id, typ):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'INSERT OR IGNORE INTO notifs_envoyees VALUES (?,?,?,?)',
        (chat_id, event_id, typ, now),
    )
    conn.commit()
    conn.close()


def marquer_event_passe(chat_id, event_id):
    """Marque toutes les notifs possibles d'un événement passé comme déjà
    envoyées, pour éviter tout rattrapage au démarrage."""
    for typ in ("departure", "takeoff", "landing"):
        marquer_notif(chat_id, event_id, typ)


# --- Récupération du prochain vol ----------------------------------------
def obtenir_prochain_vol_pour_pilote(pilote_chat_id):
    """Renvoie le prochain événement valide (numéro de vol + ≥2 IATA) ou None.
    Les événements déjà terminés sont marqués comme 'déjà notifiés'."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT google_token FROM pilotes WHERE chat_id = ?', (pilote_chat_id,))
    row = c.fetchone()
    conn.close()

    if not row or not row[0]:
        return None

    try:
        cred_dict = json.loads(row[0])
        creds = Credentials.from_authorized_user_info(cred_dict, SCOPES)
        service = build('calendar', 'v3', credentials=creds)
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # On récupère aussi quelques événements passés pour pouvoir les
        # marquer comme déjà notifiés (évite tout rattrapage au démarrage).
        time_min = (now_utc - datetime.timedelta(days=2)).isoformat()
        events_result = service.events().list(
            calendarId='primary', timeMin=time_min,
            maxResults=20, singleEvents=True,
            orderBy='startTime').execute()
        events = events_result.get('items', [])

        # 1) On marque immédiatement tous les événements déjà finis comme
        #    notifiés (pas de rattrapage au démarrage/redémarrage).
        for ev in events:
            ev_end = _parse_dt(ev.get('end', {}))
            ev_id = ev.get('id', '')
            if ev_end and ev_end < now_utc and ev_id:
                marquer_event_passe(pilote_chat_id, ev_id)

        # 2) On parcourt les événements encore en cours ou à venir.
        prochain = None
        for event in events:
            full_text = texte_evenement(event)
            numero_vol = extraire_numero_vol(full_text)
            iata_list = extraire_codes_iata(full_text)
            if not (numero_vol and len(iata_list) >= 2):
                continue

            debut_dt = _parse_dt(event.get('start', {}))
            fin_dt = _parse_dt(event.get('end', {}))
            if not debut_dt:
                continue

            prochain = {
                "id": event.get('id', ''),
                "numero": numero_vol,
                "titre": event.get('summary', ''),
                "iata": iata_list,
                "debut_dt": debut_dt,
                "fin_dt": fin_dt,
                "immats": extraire_immatriculations(event),
                "ag": est_evenement_ag(event),
                "event": event,
            }
            break
        return prochain
    except Exception as e:
        print(f"Erreur API Google Calendar : {e}")
        return None


def verifier_ads_b(numero_vol):
    url = "https://opensky-network.org/api/states/all"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            states = response.json().get("states", [])
            for state in states:
                callsign = state[1]
                if callsign and numero_vol.strip().upper() in callsign.strip().upper():
                    on_ground = state[8]
                    alt_ft = int(state[7] * 3.28084) if state[7] is not None else 0
                    return {
                        "etat": "Au sol 🟢" if on_ground else "En vol ✈️",
                        "altitude": f"{alt_ft} ft"
                    }
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# NOTIFICATIONS (décollage / atterrissage) avec corrections anti-futur
# ---------------------------------------------------------------------------
def _recipients(pilote_chat_id):
    """Renvoie la liste des chat_id à notifier (pilote + proches)."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT chat_id FROM proches WHERE pilote_chat_id = ?',
        (pilote_chat_id,),
    )
    proches = [r[0] for r in c.fetchall()]
    conn.close()
    return [pilote_chat_id] + proches


def _notifier(pilote_chat_id, vol, type_notif, texte):
    """Envoie un message au pilote + proches, dédoublonné."""
    for cid in _recipients(pilote_chat_id):
        if not notif_deja_envoyee(cid, vol["id"], type_notif):
            envoyer_message_telegram(cid, texte)
            marquer_notif(cid, vol["id"], type_notif)


def cycle_notifications():
    """Boucle : pour chaque pilote, vérifie les vols et notifie au bon
    moment. N'envoie jamais de notif pour un vol FUTUR. Marque les vols
    passés comme déjà notifiés au premier passage."""
    while True:
        try:
            if not TELEGRAM_TOKEN:
                time.sleep(10)
                continue

            now = datetime.datetime.now(datetime.timezone.utc)

            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute(
                "SELECT chat_id FROM pilotes "
                "WHERE google_token IS NOT NULL AND google_token != ''"
            )
            pilotes = [r[0] for r in c.fetchall()]
            conn.close()

            for pilote_id in pilotes:
                vol = obtenir_prochain_vol_pour_pilote(pilote_id)
                if not vol:
                    continue

                debut = vol["debut_dt"]
                fin = vol.get("fin_dt")
                ag = vol.get("ag", False)

                # --- GARDE-FOU ANTI-FUTUR ---
                # On ne notifie JAMAIS un événement qui n'a pas encore commencé.
                if not debut or debut > now:
                    continue

                if ag:
                    # Aviation générale : décollage initial + DERNIER
                    # atterrissage (fin d'événement). Notifs intermédiaires
                    # inhibées.
                    delta_takeoff = now - debut
                    if (
                        datetime.timedelta(0) <= delta_takeoff
                        <= datetime.timedelta(hours=FENETRE_DECOLLAGE_HEURES)
                    ):
                        iata_txt = " → ".join(format_iata(c) for c in vol["iata"][:2])
                        immats = ", ".join(vol.get("immats", [])) or "—"
                        msg = (
                            f"🛫 **Décollage initial (TDP)** — vol {vol['numero']}\n"
                            f"• Trajet : {iata_txt}\n"
                            f"• Avion : {immats}\n"
                            f"• Début : {debut.strftime('%H:%M UTC')}\n\n"
                            f"_Aviation générale : notifs intermédiaires "
                            f"inhibées (TDP={TDP_MINUTES} min)._"
                        )
                        _notifier(pilote_id, vol, "takeoff", msg)

                    # Dernier atterrissage = fin d'événement (pour AG).
                    if fin and fin <= now + datetime.timedelta(minutes=15):
                        # On ne notifie landing que si takeoff a déjà été notifié.
                        deja_takeoff = notif_deja_envoyee(pilote_id, vol["id"], "takeoff")
                        if deja_takeoff:
                            iata_txt = " → ".join(format_iata(c) for c in vol["iata"][:2])
                            immats = ", ".join(vol.get("immats", [])) or "—"
                            msg = (
                                f"🛬 **Dernier atterrissage (fin de séance)** — "
                                f"vol {vol['numero']}\n"
                                f"• Trajet : {iata_txt}\n"
                                f"• Avion : {immats}\n"
                                f"• Fin : {fin.strftime('%H:%M UTC')}"
                            )
                            _notifier(pilote_id, vol, "landing", msg)

                else:
                    # Vol classique (non AG).
                    delta_takeoff = now - debut
                    if (
                        datetime.timedelta(0) <= delta_takeoff
                        <= datetime.timedelta(hours=FENETRE_DECOLLAGE_HEURES)
                    ):
                        eta_txt = fin.strftime('%H:%M UTC') if fin else "?"
                        iata_txt = " → ".join(format_iata(c) for c in vol["iata"][:2])
                        immats = ", ".join(vol.get("immats", [])) or "—"
                        msg = (
                            f"🛫 **Vol décollé** — {vol['numero']}\n"
                            f"• Trajet : {iata_txt}\n"
                            f"• Avion : {immats}\n"
                            f"• Atterrissage prévu : {eta_txt}"
                        )
                        _notifier(pilote_id, vol, "takeoff", msg)

                    if (
                        fin
                        and now >= fin
                        and notif_deja_envoyee(pilote_id, vol["id"], "takeoff")
                    ):
                        iata_txt = " → ".join(format_iata(c) for c in vol["iata"][:2])
                        immats = ", ".join(vol.get("immats", [])) or "—"
                        msg = (
                            f"🛬 **Vol atterri** — {vol['numero']}\n"
                            f"• Trajet : {iata_txt}\n"
                            f"• Avion : {immats}"
                        )
                        _notifier(pilote_id, vol, "landing", msg)

        except Exception as e:
            print(f"Erreur cycle notifs : {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# BOUCLE PRINCIPALE DU BOT TELEGRAM (commandes + callback_query)
# ---------------------------------------------------------------------------
# --- LISTE DES HANDLERS (pour logging au démarrage) ---
HANDLERS_COMMANDS = [
    "/start", "/help", "/register", "/moncode", "/invite",
    "/join", "/stop", "/desabonner", "/vol", "/status",
]
HANDLERS_CALLBACKS = [
    "stop:<pilote_id>", "stop:cancel",
]


def _log_exception(context, exc):
    """Centralise le log des exceptions (équivalent error_handler)."""
    import traceback as _tb
    print(f"[bot_vol][ERROR] {context}: {type(exc).__name__}: {exc}")
    _tb.print_exc()


def error_handler(context, exception):
    """Error_handler global — journalise toute exception non rattrapée."""
    _log_exception(context, exception)


def _traiter_callback_query(query):
    """Traite les callback_query Telegram (réponse aux boutons inline)."""
    cb_id = query.get("id")
    data = query.get("data", "")
    chat_id = (
        query.get("message", {}).get("chat", {}).get("id")
        if query.get("message")
        else None
    )
    # Acquittement (obligatoire pour faire disparaître le sablier).
    # Toujours appelé en premier, même si data est inconnu.
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cb_id},
            timeout=5,
        )
    except Exception as e:
        _log_exception("callback.answer", e)

    if not chat_id:
        return

    try:
        if data.startswith("stop:"):
            payload = data.split(":", 1)[1]
            if payload == "cancel":
                envoyer_message_telegram(chat_id, "❌ Désabonnement annulé.")
                return
            try:
                pilote_id = int(payload)
            except ValueError:
                envoyer_message_telegram(
                    chat_id, "⚠️ Bouton invalide."
                )
                return
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute(
                'DELETE FROM proches WHERE chat_id = ? AND pilote_chat_id = ?',
                (chat_id, pilote_id),
            )
            deleted = c.rowcount
            conn.commit()
            conn.close()
            if deleted:
                envoyer_message_telegram(
                    chat_id, "✅ Vous êtes désabonné de ce pilote."
                )
            else:
                envoyer_message_telegram(
                    chat_id, "ℹ️ Vous n'étiez pas abonné à ce pilote."
                )
        else:
            # Bouton inconnu → on confirme silencieusement (déjà fait via
            # answerCallbackQuery) mais on informe l'utilisateur.
            envoyer_message_telegram(
                chat_id, "⚠️ Action inconnue."
            )
    except Exception as e:
        _log_exception("callback.dispatch", e)
        try:
            envoyer_message_telegram(
                chat_id, "⚠️ Une erreur est survenue, réessayez."
            )
        except Exception:
            pass


def lancer_bot_interactif():
    """Boucle principale : getUpdates + dispatch message / callback_query."""
    print("🤖 Bot Telegram Multi-utilisateurs démarré...")
    # Tente de récupérer BOT_USERNAME via getMe() au démarrage.
    _resolve_bot_username()
    if BOT_USERNAME:
        print(f"[bot_vol] BOT_USERNAME actif : {BOT_USERNAME}")
    else:
        print("[bot_vol] ⚠️ BOT_USERNAME introuvable : "
              "liens d'invitation indisponibles.")

    # --- Log de démarrage : liste explicite des handlers ---
    print("[bot_vol] Handlers commandes : "
          + ", ".join(HANDLERS_COMMANDS))
    print("[bot_vol] Handlers callbacks : "
          + ", ".join(HANDLERS_CALLBACKS))

    def _safe(callable_, *args, **kwargs):
        """Exécute un handler en isolant les exceptions."""
        try:
            return callable_(*args, **kwargs)
        except Exception as e:
            _log_exception("handler", e)
            return None

    offset = 0
    while True:
        try:
            if not TELEGRAM_TOKEN:
                time.sleep(10)
                continue

            url = (
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
                f"?offset={offset}&timeout=30"
            )
            response = requests.get(url, timeout=35)
            data = response.json()

            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    # 1) Callback query (boutons inline)
                    if "callback_query" in update:
                        _traiter_callback_query(update["callback_query"])
                        continue

                    # 2) Message texte classique
                    if "message" not in update or "text" not in update["message"]:
                        continue

                    chat_id = update["message"]["chat"]["id"]
                    texte = update["message"]["text"].strip()

                    # --- /start [CODE] ---
                    if texte.startswith("/start"):
                        parts = texte.split(maxsplit=1)
                        if len(parts) == 2 and parts[1].strip():
                            code_saisi = parts[1].strip().upper()
                            conn = sqlite3.connect(DB_FILE)
                            c = conn.cursor()
                            c.execute(
                                'SELECT chat_id FROM pilotes '
                                'WHERE code_invitation = ?',
                                (code_saisi,),
                            )
                            pilote = c.fetchone()
                            if pilote:
                                pilote_id = pilote[0]
                                c.execute('''
                                    INSERT INTO proches (chat_id, pilote_chat_id)
                                    VALUES (?, ?)
                                    ON CONFLICT(chat_id) DO UPDATE
                                    SET pilote_chat_id = excluded.pilote_chat_id
                                ''', (chat_id, pilote_id))
                                conn.commit()
                                conn.close()
                                envoyer_message_telegram(
                                    chat_id,
                                    f"✅ Vous êtes rattaché au pilote ! "
                                    f"Tapez /vol pour voir les vols."
                                )
                            else:
                                conn.close()
                                envoyer_message_telegram(
                                    chat_id,
                                    f"❌ Code d'invitation `{code_saisi}` invalide."
                                )
                            continue
                        # /start sans payload → message d'accueil.
                        envoyer_message_telegram(chat_id, (
                            "👋 **Bienvenue sur le Bot de Suivi de Vol Multi-Pilotes !**\n\n"
                            "👨‍✈️ **Vous êtes Pilote ?**\n"
                            "• Tapez `/register` pour lier votre Google Calendar.\n"
                            "• Tapez `/moncode` pour voir votre code d'invitation.\n"
                            "• Tapez `/invite` pour le lien à partager.\n\n"
                            "👪 **Vous êtes Proche/Famille ?**\n"
                            "• Cliquez sur le lien d'invitation reçu, ou tapez "
                            "`/join CODE` pour vous abonner.\n"
                            "• Tapez `/vol` pour consulter le prochain vol.\n"
                            "• Tapez `/stop` pour vous désabonner.\n\n"
                            "📚 Tapez `/help` pour l'aide complète (FR/EN)."
                        ))

                    # --- /help ---
                    elif texte.startswith("/help"):
                        envoyer_message_telegram(chat_id, (
                            "📚 **Aide — FR**\n\n"
                            "*Pilote :*\n"
                            "• `/register` — lier Google Calendar\n"
                            "• `/moncode` — afficher le code d'invitation\n"
                            "• `/invite` — afficher le lien à partager\n\n"
                            "*Proche :*\n"
                            "• `/join CODE` — s'abonner à un pilote\n"
                            "• `/vol` — voir le prochain vol\n"
                            "• `/status` — état du vol\n"
                            "• `/stop` (alias `/desabonner`) — se désabonner\n\n"
                            "📚 **Help — EN**\n\n"
                            "*Pilot:*\n"
                            "• `/register` — connect Google Calendar\n"
                            "• `/moncode` — show invitation code\n"
                            "• `/invite` — show shareable link\n\n"
                            "*Contact:*\n"
                            "• `/join CODE` — subscribe to a pilot\n"
                            "• `/vol` — view next flight\n"
                            "• `/status` — flight status\n"
                            "• `/stop` — unsubscribe"
                        ))

                    # --- /register ---
                    elif texte.startswith("/register") or texte.startswith("/register@"):
                        link = f"{RENDER_EXTERNAL_URL}/login?chat_id={chat_id}"
                        envoyer_message_telegram(chat_id, (
                            "🔗 *Connexion Pilote Google Calendar :*\n\n"
                            "Cliquez sur ce lien pour autoriser l'accès à votre agenda :\n"
                            f"{link}"
                        ))

                    # --- /moncode ---
                    elif texte.startswith("/moncode"):
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute(
                            'SELECT code_invitation FROM pilotes '
                            'WHERE chat_id = ?',
                            (chat_id,),
                        )
                        row = c.fetchone()
                        conn.close()
                        if row and row[0]:
                            invite = _build_invite_link(row[0])
                            if invite:
                                envoyer_message_telegram(chat_id, (
                                    f"🔑 Votre code d'invitation : `{row[0]}`\n"
                                    f"Lien à partager : {invite}"
                                ))
                            else:
                                envoyer_message_telegram(chat_id, (
                                    f"🔑 Votre code d'invitation proche est : "
                                    f"`{row[0]}`"
                                ))
                        else:
                            envoyer_message_telegram(chat_id, (
                                "❌ Vous n'êtes pas encore enregistré. "
                                "Tapez `/register` d'abord."
                            ))

                    # --- /invite ---
                    elif texte.startswith("/invite"):
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute(
                            'SELECT code_invitation FROM pilotes '
                            'WHERE chat_id = ?',
                            (chat_id,),
                        )
                        row = c.fetchone()
                        conn.close()
                        if row and row[0]:
                            invite = _build_invite_link(row[0])
                            if invite:
                                envoyer_message_telegram(chat_id, (
                                    f"🔗 Lien d'invitation à partager :\n{invite}"
                                ))
                            else:
                                envoyer_message_telegram(chat_id, (
                                    f"⚠️ BOT_USERNAME non configuré. "
                                    f"Partagez plutôt le code `{row[0]}` "
                                    f"avec `/join {row[0]}`."
                                ))
                        else:
                            envoyer_message_telegram(chat_id, (
                                "❌ Vous devez vous enregistrer d'abord "
                                "(`/register`)."
                            ))

                    # --- /join CODE ---
                    elif texte.startswith("/join"):
                        parts = texte.split()
                        if len(parts) < 2:
                            envoyer_message_telegram(chat_id, (
                                "⚠️ Format incorrect. Tapez : "
                                "`/join CODE` (ex: `/join RAF123`)"
                            ))
                            continue
                        code_saisi = parts[1].strip().upper()
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute(
                            'SELECT chat_id FROM pilotes '
                            'WHERE code_invitation = ?',
                            (code_saisi,),
                        )
                        pilote = c.fetchone()
                        if pilote:
                            pilote_id = pilote[0]
                            c.execute('''
                                INSERT INTO proches (chat_id, pilote_chat_id)
                                VALUES (?, ?)
                                ON CONFLICT(chat_id) DO UPDATE
                                SET pilote_chat_id = excluded.pilote_chat_id
                            ''', (chat_id, pilote_id))
                            conn.commit()
                            conn.close()
                            envoyer_message_telegram(chat_id, (
                                f"✅ Vous êtes rattaché au pilote ! "
                                f"Tapez `/vol` pour voir les vols."
                            ))
                        else:
                            conn.close()
                            envoyer_message_telegram(
                                chat_id, "❌ Code d'invitation invalide."
                            )

                    # --- /stop / /desabonner ---
                    elif texte.startswith("/stop") or texte.startswith("/desabonner"):
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute(
                            'SELECT pilote_chat_id FROM proches '
                            'WHERE chat_id = ?',
                            (chat_id,),
                        )
                        rows = c.fetchall()
                        conn.close()
                        if not rows:
                            envoyer_message_telegram(chat_id, (
                                "ℹ️ Vous n'êtes abonné à aucun pilote."
                            ))
                            continue
                        if len(rows) == 1:
                            pilote_id = rows[0][0]
                            conn = sqlite3.connect(DB_FILE)
                            c = conn.cursor()
                            c.execute(
                                'DELETE FROM proches WHERE chat_id = ?',
                                (chat_id,),
                            )
                            conn.commit()
                            conn.close()
                            envoyer_message_telegram(chat_id, (
                                "✅ Vous êtes désabonné. Vous ne recevrez "
                                "plus de notifications pour ce pilote."
                            ))
                        else:
                            # Plusieurs pilotes → boutons inline.
                            boutons = []
                            for pilote_id in rows:
                                conn = sqlite3.connect(DB_FILE)
                                c = conn.cursor()
                                c.execute(
                                    'SELECT code_invitation FROM pilotes '
                                    'WHERE chat_id = ?',
                                    (pilote_id,),
                                )
                                code_row = c.fetchone()
                                code_label = code_row[0] if code_row else str(pilote_id)
                                conn.close()
                                boutons.append([{
                                    "text": f"❌ Se désabonner du pilote {code_label}",
                                    "callback_data": f"stop:{pilote_id}",
                                }])
                            boutons.append([
                                {"text": "Annuler", "callback_data": "stop:cancel"}
                            ])
                            envoyer_boutons_telegram(
                                chat_id,
                                "Vous êtes abonné à plusieurs pilotes. "
                                "Choisissez celui dont vous voulez vous "
                                "désabonner :",
                                boutons,
                            )

                    # --- /vol / /status ---
                    elif texte.startswith("/vol") or texte.startswith("/status"):
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute(
                            'SELECT pilote_chat_id FROM proches '
                            'WHERE chat_id = ?',
                            (chat_id,),
                        )
                        row_proche = c.fetchone()
                        pilote_target_id = None
                        if row_proche:
                            pilote_target_id = row_proche[0]
                        else:
                            c.execute(
                                'SELECT chat_id FROM pilotes '
                                'WHERE chat_id = ?',
                                (chat_id,),
                            )
                            row_pilote = c.fetchone()
                            if row_pilote:
                                pilote_target_id = row_pilote[0]
                        conn.close()
                        if not pilote_target_id:
                            envoyer_message_telegram(chat_id, (
                                "🔒 **Accès restreint.**\n"
                                "Entrez le code d'invitation d'un pilote avec "
                                "`/join CODE` pour accéder à ses vols."
                            ))
                            continue
                        envoyer_message_telegram(chat_id, "🔍 Recherche du prochain vol...")
                        vol = obtenir_prochain_vol_pour_pilote(pilote_target_id)
                        if not vol:
                            envoyer_message_telegram(
                                chat_id, "❌ Aucun vol trouvé dans l'agenda."
                            )
                        else:
                            live = verifier_ads_b(vol['numero'])
                            statut_live = (
                                f"État : {live['etat']} | "
                                f"Altitude : {live['altitude']}"
                                if live else "Avion non détecté sur le radar."
                            )
                            iata_txt = " → ".join(
                                format_iata(c) for c in vol.get("iata", [])[:2]
                            ) or "—"
                            immats = ", ".join(vol.get("immats", [])) or "—"
                            link_fr24 = (
                                f"https://www.flightradar24.com/data/flights/"
                                f"{vol['numero']}"
                            )
                            ag_note = " (Tours de piste)" if vol.get("ag") else ""
                            envoyer_message_telegram(chat_id, (
                                f"✈️ **Prochain vol{ag_note} :**\n\n"
                                f"• **Vol :** {vol['titre']}\n"
                                f"• **Numéro :** {vol['numero']}\n"
                                f"• **Trajet :** {iata_txt}\n"
                                f"• **Avion (immat.) :** {immats}\n"
                                f"• **Départ prévu :** "
                                f"{vol['debut_dt'].strftime('%d/%m/%Y à %H:%M UTC')}\n\n"
                                f"📡 **Statut radar :**\n{statut_live}\n\n"
                                f"🔗 [Suivre sur Flightradar24]({link_fr24})"
                            ))

        except Exception as e:
            print(f"Erreur boucle bot : {e}")
            time.sleep(5)
        time.sleep(2)


# ---------------------------------------------------------------------------
# Lancement global propre (Bot + Notifications en arrière-plan + Flask)
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    # 1) Démarre la boucle de notifications (dédoublonnée, anti-futur).
    threading.Thread(target=cycle_notifications, daemon=True).start()
    # 2) Démarre la boucle du bot Telegram (commandes + callbacks).
    threading.Thread(target=lancer_bot_interactif, daemon=True).start()
    # 3) Flask au premier plan.
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)