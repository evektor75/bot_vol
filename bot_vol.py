import os
import re
import json
import time
import html
import sqlite3
import random
import string
import secrets
import datetime
import threading
import requests
from flask import Flask, request, redirect
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLIENT_SECRET_JSON = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000").rstrip("/")

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# Si tu as un disque persistant sur Render, mets DB_PATH=/chemin/du/disque/bot_vols.db
DB_FILE = os.environ.get("DB_PATH", "bot_vols.db")

# Stockage temporaire des "state" OAuth : {state_token: (chat_id, timestamp)}
OAUTH_STATES = {}
OAUTH_STATE_TTL = 600  # 10 minutes
oauth_lock = threading.Lock()

# ---------------------------------------------------------------------------
# BASE DE DONNÉES SQLITE
# ---------------------------------------------------------------------------
def init_db():
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
    conn.commit()
    conn.close()

init_db()

def generer_code_invitation():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

# ---------------------------------------------------------------------------
# TELEGRAM : ENVOI
# ---------------------------------------------------------------------------
def envoyer_message_telegram(chat_id, texte, html_mode=True):
    """Envoie un message. Mode HTML par défaut, avec repli en texte brut si Telegram refuse."""
    if not TELEGRAM_TOKEN:
        print("⚠️ ERREUR: TELEGRAM_TOKEN manquant, impossible d'envoyer un message")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texte,
        "disable_web_page_preview": False
    }
    if html_mode:
        payload["parse_mode"] = "HTML"

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            return True

        print(f"⚠️ Erreur Telegram {r.status_code}: {r.text}")

        # Repli : texte brut sans balises HTML
        if html_mode:
            payload.pop("parse_mode", None)
            payload["text"] = re.sub(r'<[^>]+>', '', html.unescape(texte))
            r2 = requests.post(url, json=payload, timeout=10)
            print(f"↩️ Repli en texte brut : {r2.status_code}")
            return r2.ok
    except Exception as e:
        print(f"⚠️ Erreur envoi Telegram : {e}")
    return False

# ---------------------------------------------------------------------------
# ROUTES FLASK (OAUTH2 & SANTÉ)
# ---------------------------------------------------------------------------
@app.route('/')
def home():
    return "🤖 Bot Telegram & OAuth actif !"

@app.route('/login')
def login():
    chat_id_raw = request.args.get('chat_id')
    if not chat_id_raw or not chat_id_raw.lstrip('-').isdigit() or not CLIENT_SECRET_JSON:
        return "Erreur : paramètres d'authentification manquants.", 400

    chat_id = int(chat_id_raw)

    # State aléatoire à usage unique (protection CSRF)
    state_token = secrets.token_urlsafe(32)
    with oauth_lock:
        # Nettoyage des anciens states expirés
        maintenant = time.time()
        for k in [k for k, (_, ts) in OAUTH_STATES.items() if maintenant - ts > OAUTH_STATE_TTL]:
            OAUTH_STATES.pop(k, None)
        OAUTH_STATES[state_token] = (chat_id, maintenant)

    client_config = json.loads(CLIENT_SECRET_JSON)
    redirect_uri = f"{RENDER_EXTERNAL_URL}/oauth2callback"

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

    auth_url, _ = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        state=state_token
    )
    return redirect(auth_url)

@app.route('/oauth2callback')
def oauth2callback():
    state_token = request.args.get('state')
    code = request.args.get('code')

    if not state_token or not code or not CLIENT_SECRET_JSON:
        return "Échec de l'authentification.", 400

    # Vérification et consommation du state
    with oauth_lock:
        entree = OAUTH_STATES.pop(state_token, None)

    if not entree:
        return "Session expirée ou invalide. Relancez /register sur Telegram.", 400

    chat_id, timestamp = entree
    if time.time() - timestamp > OAUTH_STATE_TTL:
        return "Lien expiré. Relancez /register sur Telegram.", 400

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

        envoyer_message_telegram(
            chat_id,
            f"✅ <b>Compte Google connecté avec succès !</b>\n\n"
            f"Votre code d'invitation pour vos proches est : <code>{code_inv}</code>\n"
            f"Partagez-leur ce code pour qu'ils s'abonnent avec :\n<code>/join {code_inv}</code>"
        )

        return "<h1>Connexion réussie !</h1><p>Vous pouvez fermer cette fenêtre et retourner sur Telegram.</p>"
    except Exception as e:
        print(f"⚠️ Erreur OAuth : {e}")
        return f"Erreur lors de la validation OAuth : {e}", 500

# ---------------------------------------------------------------------------
# LOGIQUE VOLS
# ---------------------------------------------------------------------------
def extraire_numero_vol(titre_event):
    match = re.search(r'\b([A-Z0-9]{2,3}\s?\d{1,4})\b', titre_event, re.IGNORECASE)
    return match.group(1).replace(" ", "").upper() if match else None

def parser_date_google(date_str):
    """Convertit une date Google Calendar (avec ou sans heure) en datetime."""
    if date_str.endswith("Z"):
        date_str = date_str[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(date_str)

def obtenir_prochain_vol_pour_pilote(pilote_chat_id):
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
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        events_result = service.events().list(
            calendarId='primary', timeMin=now,
            maxResults=10, singleEvents=True,
            orderBy='startTime').execute()
        events = events_result.get('items', [])

        for event in events:
            titre = event.get('summary', '')
            numero_vol = extraire_numero_vol(titre)

            if numero_vol or "VOL" in titre.upper() or re.search(r'\b[A-Z]{3}-[A-Z]{3}\b', titre):
                debut_str = event['start'].get('dateTime', event['start'].get('date'))
                debut_dt = parser_date_google(debut_str)
                if not numero_vol:
                    match_fallback = re.search(r'^([A-Z0-9]+)', titre)
                    numero_vol = match_fallback.group(1) if match_fallback else titre

                return {
                    "numero": numero_vol,
                    "titre": titre,
                    "debut_dt": debut_dt
                }
        return None
    except Exception as e:
        print(f"⚠️ Erreur API Google Calendar : {e}")
        return None

def verifier_ads_b(numero_vol):
    url = "https://opensky-network.org/api/states/all"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            states = response.json().get("states") or []
            for state in states:
                callsign = state[1]
                if callsign and numero_vol.strip().upper() in callsign.strip().upper():
                    on_ground = state[8]
                    alt_ft = int(state[7] * 3.28084) if state[7] is not None else 0
                    return {
                        "etat": "Au sol 🟢" if on_ground else "En vol ✈️",
                        "altitude": f"{alt_ft} ft"
                    }
    except Exception as e:
        print(f"⚠️ Erreur OpenSky : {e}")
    return None

# ---------------------------------------------------------------------------
# COMMANDES TELEGRAM
# ---------------------------------------------------------------------------
def cmd_start(chat_id):
    msg = (
        "👋 <b>Bienvenue sur le Bot de Suivi de Vol Multi-Pilotes !</b>\n\n"
        "👨‍✈️ <b>Vous êtes Pilote ?</b>\n"
        "• Tapez /register pour lier votre Google Calendar.\n"
        "• Tapez /moncode pour voir votre code d'invitation.\n\n"
        "👪 <b>Vous êtes Proche/Famille ?</b>\n"
        "• Tapez <code>/join CODE</code> pour vous abonner à un pilote (ex: <code>/join RAF123</code>).\n"
        "• Tapez /vol pour consulter le prochain vol de votre pilote !"
    )
    envoyer_message_telegram(chat_id, msg)

def cmd_register(chat_id):
    link = f"{RENDER_EXTERNAL_URL}/login?chat_id={chat_id}"
    msg = (
        "🔗 <b>Connexion Pilote Google Calendar :</b>\n\n"
        "Cliquez sur ce lien pour autoriser l'accès à votre agenda :\n"
        f"{link}"
    )
    ok = envoyer_message_telegram(chat_id, msg)
    print(f"DEBUG: lien /register envoyé (succès={ok}) -> {link}")

def cmd_moncode(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT code_invitation FROM pilotes WHERE chat_id = ?', (chat_id,))
    row = c.fetchone()
    conn.close()

    if row and row[0]:
        envoyer_message_telegram(chat_id, f"🔑 Votre code d'invitation proche est : <code>{row[0]}</code>")
    else:
        envoyer_message_telegram(chat_id, "❌ Vous n'êtes pas encore enregistré. Tapez /register d'abord.")

def cmd_join(chat_id, args):
    if not args:
        envoyer_message_telegram(chat_id, "⚠️ Format incorrect. Tapez : <code>/join CODE</code> (ex: <code>/join RAF123</code>)")
        return

    code_saisi = args[0].strip().upper()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT chat_id FROM pilotes WHERE code_invitation = ?', (code_saisi,))
    pilote = c.fetchone()

    if pilote:
        pilote_id = pilote[0]
        c.execute('''
            INSERT INTO proches (chat_id, pilote_chat_id)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET pilote_chat_id = excluded.pilote_chat_id
        ''', (chat_id, pilote_id))
        conn.commit()
        conn.close()
        envoyer_message_telegram(chat_id, "✅ Vous êtes rattaché au pilote ! Tapez /vol pour voir les vols.")
    else:
        conn.close()
        envoyer_message_telegram(chat_id, "❌ Code d'invitation invalide.")

def cmd_vol(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT pilote_chat_id FROM proches WHERE chat_id = ?', (chat_id,))
    row_proche = c.fetchone()

    pilote_target_id = None
    if row_proche:
        pilote_target_id = row_proche[0]
    else:
        c.execute('SELECT chat_id FROM pilotes WHERE chat_id = ?', (chat_id,))
        row_pilote = c.fetchone()
        if row_pilote:
            pilote_target_id = row_pilote[0]
    conn.close()

    if not pilote_target_id:
        envoyer_message_telegram(
            chat_id,
            "🔒 <b>Accès restreint.</b>\n"
            "Entrez le code d'invitation d'un pilote avec <code>/join CODE</code> pour accéder à ses vols."
        )
        return

    envoyer_message_telegram(chat_id, "🔍 Recherche du prochain vol...")
    vol = obtenir_prochain_vol_pour_pilote(pilote_target_id)

    if not vol:
        envoyer_message_telegram(chat_id, "❌ Aucun vol trouvé dans l'agenda.")
        return

    live = verifier_ads_b(vol['numero'])
    if live:
        statut_live = f"État : {live['etat']} | Altitude : {live['altitude']}"
    else:
        statut_live = "Avion non détecté sur le radar."

    link_fr24 = f"https://www.flightradar24.com/data/flights/{vol['numero']}"
    titre_safe = html.escape(vol['titre'])

    reponse = (
        f"✈️ <b>Prochain vol :</b>\n\n"
        f"• <b>Vol :</b> {titre_safe}\n"
        f"• <b>Départ prévu :</b> {vol['debut_dt'].strftime('%d/%m/%Y à %H:%M')}\n\n"
        f"📡 <b>Statut radar :</b>\n{statut_live}\n\n"
        f'🔗 <a href="{link_fr24}">Suivre sur Flightradar24</a>'
    )
    envoyer_message_telegram(chat_id, reponse)

def traiter_message(chat_id, texte):
    """Route une commande vers sa fonction (gère /commande@NomDuBot)."""
    if not texte.startswith("/"):
        return

    parts = texte.split()
    commande = parts[0].split("@")[0].lower()
    args = parts[1:]

    if commande == "/start":
        cmd_start(chat_id)
    elif commande == "/register":
        cmd_register(chat_id)
    elif commande == "/moncode":
        cmd_moncode(chat_id)
    elif commande == "/join":
        cmd_join(chat_id, args)
    elif commande == "/vol":
        cmd_vol(chat_id)
    else:
        envoyer_message_telegram(chat_id, "❓ Commande inconnue. Tapez /start pour voir l'aide.")

# ---------------------------------------------------------------------------
# BOUCLE DE POLLING TELEGRAM
# ---------------------------------------------------------------------------
def lancer_bot_interactif():
    print("🤖 Bot Telegram Multi-utilisateurs démarré...")

    if not TELEGRAM_TOKEN:
        print("⚠️ TELEGRAM_TOKEN non défini ! Le bot ne peut pas démarrer.")
    else:
        # Supprime un éventuel webhook qui bloquerait getUpdates
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook", timeout=10)
            print(f"deleteWebhook : {r.text}")
        except Exception as e:
            print(f"⚠️ Erreur deleteWebhook : {e}")

    offset = 0

    while True:
        try:
            if not TELEGRAM_TOKEN:
                time.sleep(10)
                continue

            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={offset}&timeout=30"
            response = requests.get(url, timeout=35)
            data = response.json()

            if not data.get("ok"):
                print(f"⚠️ Erreur getUpdates : {data}")
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message")
                if not message or "text" not in message:
                    continue

                chat_id = message["chat"]["id"]
                texte = message["text"].strip()
                print(f"DEBUG: message reçu de {chat_id} : {texte!r}")

                try:
                    traiter_message(chat_id, texte)
                except Exception as e:
                    print(f"⚠️ Erreur traitement commande {texte!r} : {e}")
                    envoyer_message_telegram(chat_id, "⚠️ Une erreur est survenue, réessayez plus tard.")

        except Exception as e:
            print(f"⚠️ Erreur boucle bot : {e}")
            time.sleep(5)

        time.sleep(1)

# ---------------------------------------------------------------------------
# LANCEMENT
# ---------------------------------------------------------------------------
# Démarrage du thread de polling (aussi sous gunicorn, pas seulement en direct)
_bot_thread_started = False

def demarrer_bot_une_fois():
    global _bot_thread_started
    if not _bot_thread_started:
        _bot_thread_started = True
        threading.Thread(target=lancer_bot_interactif, daemon=True).start()

demarrer_bot_une_fois()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)