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
from flask import Flask, request, redirect, url_for
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# --- CONFIGURATION SÉCURISÉE ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLIENT_SECRET_JSON = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

# --- LOGS DE DÉMARRAGE CLAIRS ---
print("=" * 60)
print("🤖 Démarrage du bot Telegram de suivi de vols")
print("=" * 60)

if not TELEGRAM_TOKEN:
    print("❌ ERREUR : TELEGRAM_TOKEN manquant dans l'environnement !")
    print("   Le bot ne pourra ni lire ni envoyer de messages.")
else:
    print(f"✅ TELEGRAM_TOKEN présent (longueur={len(TELEGRAM_TOKEN)})")

if not RENDER_EXTERNAL_URL or RENDER_EXTERNAL_URL.startswith("http://localhost"):
    print(f"⚠️  ATTENTION : RENDER_EXTERNAL_URL non défini en prod → {RENDER_EXTERNAL_URL}")
    print("   Les liens OAuth Google seront invalides hors de localhost.")
else:
    print(f"✅ RENDER_EXTERNAL_URL = {RENDER_EXTERNAL_URL}")

if not CLIENT_SECRET_JSON:
    print("⚠️  ATTENTION : GOOGLE_CLIENT_SECRET_JSON manquant → OAuth Google désactivé.")
else:
    print("✅ GOOGLE_CLIENT_SECRET_JSON présent.")

# --- SUPPRESSION DU WEBHOOK (évite le conflit avec le polling long) ---
def supprimer_webhook_telegram():
    """Supprime tout webhook éventuellement enregistré sur Telegram.
    Indispensable quand on bascule entre webhook et long-polling,
    sinon getUpdates renvoie une liste vide et aucun message n'est reçu."""
    if not TELEGRAM_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook"
        r = requests.get(url, timeout=10)
        print(f"🧹 deleteWebhook → {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️  Erreur deleteWebhook : {e}")

# Initialisation de Flask (Serveur Web unique)
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-rafael")

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
def envoyer_message_telegram(chat_id, texte, parse_mode="Markdown"):
    """Envoie un message Telegram.

    - Vérifie TELEGRAM_TOKEN (sinon log + return False).
    - Stocke la réponse HTTP, vérifie r.ok.
    - Logue r.status_code et r.text si non-OK.
    - En cas d'échec de parsing Markdown, RETENTE automatiquement en texte brut
      pour ne pas perdre la réponse à cause d'un caractère spécial.
    """
    if not TELEGRAM_TOKEN:
        print(f"⚠️  TELEGRAM_TOKEN absent → message NON envoyé vers chat_id={chat_id}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texte,
        "disable_web_page_preview": False
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            return True

        # Échec : log complet
        print(f"❌ Telegram NON-OK (status={r.status_code}) pour chat_id={chat_id} : {r.text[:300]}")

        # Fallback automatique : si Telegram refuse le Markdown (status 400),
        # on renvoie le même texte sans parse_mode (texte brut).
        if parse_mode and r.status_code == 400 and "can't parse" in r.text.lower():
            payload_plain = {k: v for k, v in payload.items() if k != "parse_mode"}
            print(f"🔁 Fallback texte brut pour chat_id={chat_id}")
            r2 = requests.post(url, json=payload_plain, timeout=10)
            if r2.ok:
                return True
            print(f"❌ Fallback texte brut aussi échoué (status={r2.status_code}) : {r2.text[:300]}")
        return False
    except Exception as e:
        print(f"❌ Exception envoi Telegram pour chat_id={chat_id} : {e}")
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
        print(f"❌ Erreur API Google Calendar : {e}")
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
    print("🤖 Bot Telegram Multi-utilisateurs démarré (mode polling)...")
    supprimer_webhook_telegram()  # évite le conflit webhook ↔ polling

    offset = 0

    while True:
        try:
            if not TELEGRAM_TOKEN:
                print("❌ TELEGRAM_TOKEN absent — bot en attente (vérifiez vos variables d'env)")
                time.sleep(10)
                continue

            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={offset}&timeout=30"
            try:
                response = requests.get(url, timeout=35)
                data = response.json()
            except Exception as e:
                print(f"❌ Erreur getUpdates : {e}")
                time.sleep(5)
                continue

            if not data.get("ok"):
                print(f"❌ getUpdates non-OK : {data}")
                time.sleep(5)
                continue

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

                    elif texte.startswith("/register") or texte.startswith("/register@"):
                        # ===== UN SEUL ENVOI =====
                        print(f"DEBUG: Commande /register interceptée pour chat_id={chat_id}")
                        link = f"{RENDER_EXTERNAL_URL}/login?chat_id={chat_id}"
                        message_texte = (
                            "🔗 **Connexion Pilote Google Calendar :**\n\n"
                            f"Cliquez sur ce lien pour autoriser l'accès à votre agenda :\n{link}"
                        )
                        envoyer_message_telegram(chat_id, message_texte)
                        print(f"DEBUG: Lien /register envoyé avec succès -> {link}")

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
                            envoyer_message_telegram(chat_id, "✅ Vous êtes rattaché au pilote ! Tapez `/vol` pour voir les vols.")
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

        except Exception as e:
            print(f"❌ Erreur boucle bot : {e}")
            time.sleep(5)
        time.sleep(2)


# Lancement global propre (Bot en arrière-plan + Flask au premier plan)
if __name__ == '__main__':
    threading.Thread(target=lancer_bot_interactif, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)