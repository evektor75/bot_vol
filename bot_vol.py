import os
import re
import json
import time
import sqlite3
import random
import string
import datetime
import threading
import requests
import logging
from flask import Flask, request, redirect, url_for
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# --- CONFIGURATION SÉCURISÉE ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLIENT_SECRET_JSON = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

# Initialisation de Flask (Serveur Web unique)
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-rafael")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("bot_vol")

# Stockage temporaire des code_verifier PKCE, indexé par state
pending_verifiers = {}

# ---------------------------------------------------------------------------
# BASE DE DONNÉES SQLITE
# ---------------------------------------------------------------------------
DB_FILE = "bot_vols.db"

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
    pending_verifiers[state] = flow.code_verifier
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
        code_verifier = pending_verifiers.pop(chat_id, None)
        if not code_verifier:
            raise RuntimeError("code_verifier PKCE introuvable pour cet état OAuth; relancez /register")
        flow.code_verifier = code_verifier
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
            f"✅ **Compte Google connecté avec succès !**\n\n"
            f"Votre code d'invitation pour vos proches est : `{code_inv}`\n"
            f"Partagez-leur ce code pour qu'ils s'abonnent avec :\n`/join {code_inv}`"
        )

        return "<h1>Connexion réussie !</h1><p>Vous pouvez fermer cette fenêtre et retourner sur Telegram.</p>"
    except Exception as e:
        return f"Erreur lors de la validation OAuth : {e}", 500

# ---------------------------------------------------------------------------
# LOGIQUE BOT TELEGRAM
# ---------------------------------------------------------------------------
def envoyer_message_telegram(chat_id, texte):
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN absent: impossible d'envoyer un message à chat_id=%s", chat_id)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texte,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if not response.ok:
            logger.error("Telegram sendMessage a échoué (%s): %s", response.status_code, response.text[:500])
            return False
        return True
    except Exception:
        logger.exception("Erreur envoi Telegram pour chat_id=%s", chat_id)
        return False

def traiter_commande_register(chat_id):
    """Construit et envoie le lien OAuth; toute erreur est loggée et signalée."""
    try:
        logger.info("Commande /register reçue pour chat_id=%s", chat_id)
        if not RENDER_EXTERNAL_URL:
            raise RuntimeError("RENDER_EXTERNAL_URL est vide")
        link = f"{RENDER_EXTERNAL_URL}/login?chat_id={chat_id}"
        message_texte = (
            "🔗 *Connexion Pilote Google Calendar :*\n\n"
            "Cliquez sur ce lien pour autoriser l'accès à votre agenda :\n"
            f"{link}"
        )
        if not envoyer_message_telegram(chat_id, message_texte):
            raise RuntimeError("Telegram n'a pas confirmé l'envoi du lien /register")
        logger.info("Lien /register envoyé pour chat_id=%s: %s", chat_id, link)
        return True
    except Exception as exc:
        logger.exception("Erreur pendant le traitement de /register pour chat_id=%s", chat_id)
        try:
            envoyer_message_telegram(
                chat_id,
                f"❌ Erreur pendant /register : {exc}\nConsultez les logs du bot pour le détail."
            )
        except Exception:
            logger.exception("Impossible de signaler l'erreur /register à chat_id=%s", chat_id)
        return False

def extraire_numero_vol(titre_event):
    match = re.search(r'\b([A-Z0-9]{2,3}\s?\d{1,4})\b', titre_event, re.IGNORECASE)
    return match.group(1).replace(" ", "").upper() if match else None

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
        now = datetime.datetime.utcnow().isoformat() + 'Z'

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
                debut_dt = datetime.datetime.fromisoformat(debut_str)
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

def lancer_bot_interactif():
    print("🤖 Bot Telegram Multi-utilisateurs démarré...")
    offset = 0

    while True:
        try:
            if not TELEGRAM_TOKEN:
                time.sleep(10)
                continue

            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={offset}&timeout=30"
            response = requests.get(url, timeout=35)
            data = response.json()

            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    if "message" in update and "text" in update["message"]:
                        chat_id = update["message"]["chat"]["id"]
                        texte = update["message"]["text"].strip()

                        if texte.startswith("/start"):
                            msg = (
                                "👋 **Bienvenue sur le Bot de Suivi de Vol Multi-Pilotes !**\n\n"
                                "👨‍✈️ **Vous êtes Pilote ?**\n"
                                "• Tapez `/register` pour lier votre Google Calendar.\n"
                                "• Tapez `/moncode` pour voir votre code d'invitation.\n\n"
                                "👪 **Vous êtes Proche/Famille ?**\n"
                                "• Tapez `/join CODE` pour vous abonner à un pilote (ex: `/join RAF123`).\n"
                                "• Tapez `/vol` pour consulter le prochain vol de votre pilote !"
                            )
                            envoyer_message_telegram(chat_id, msg)

                        elif re.match(r"^/register(?:@\w+)?(?:\s|$)", texte, re.IGNORECASE):
                            traiter_commande_register(chat_id)

                        elif texte.startswith("/moncode"):
                            conn = sqlite3.connect(DB_FILE)
                            c = conn.cursor()
                            c.execute('SELECT code_invitation FROM pilotes WHERE chat_id = ?', (chat_id,))
                            row = c.fetchone()
                            conn.close()

                            if row and row[0]:
                                envoyer_message_telegram(chat_id, f"🔑 Votre code d'invitation proche est : `{row[0]}`")
                            else:
                                envoyer_message_telegram(chat_id, "❌ Vous n'êtes pas encore enregistré. Tapez `/register` d'abord.")

                        elif texte.startswith("/join"):
                            parts = texte.split()
                            if len(parts) < 2:
                                envoyer_message_telegram(chat_id, "⚠️ Format incorrect. Tapez : `/join CODE` (ex: `/join RAF123`)")
                                continue

                            code_saisi = parts[1].strip().upper()
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
                                envoyer_message_telegram(chat_id, f"✅ Vous êtes rattaché au pilote ! Tapez `/vol` pour voir les vols.")
                            else:
                                conn.close()
                                envoyer_message_telegram(chat_id, "❌ Code d'invitation invalide.")

                        elif texte.startswith("/vol"):
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
                                    "🔒 **Accès restreint.**\n"
                                    "Entrez le code d'invitation d'un pilote avec `/join CODE` pour accéder à ses vols."
                                )
                                continue

                            envoyer_message_telegram(chat_id, "🔍 Recherche du prochain vol...")
                            vol = obtenir_prochain_vol_pour_pilote(pilote_target_id)

                            if not vol:
                                envoyer_message_telegram(chat_id, "❌ Aucun vol trouvé dans l'agenda.")
                            else:
                                live = verifier_ads_b(vol['numero'])
                                statut_live = f"État : {live['etat']} | Altitude : {live['altitude']}" if live else "Avion non détecté sur le radar."
                                link_fr24 = f"https://www.flightradar24.com/data/flights/{vol['numero']}"

                                reponse = (
                                    f"✈️ **Prochain vol :**\n\n"
                                    f"• **Vol :** {vol['titre']}\n"
                                    f"• **Départ prévu :** {vol['debut_dt'].strftime('%d/%m/%Y à %H:%M')}\n\n"
                                    f"📡 **Statut radar :**\n{statut_live}\n\n"
                                    f"🔗 [Suivre sur Flightradar24]({link_fr24})"
                                )
                                envoyer_message_telegram(chat_id, reponse)

        except Exception:
            logger.exception("Erreur boucle bot Telegram")
            time.sleep(5)
        time.sleep(2)

# Lancement global propre (Bot en arrière-plan + Flask au premier plan)
if __name__ == '__main__':
    threading.Thread(target=lancer_bot_interactif, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)