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
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

# --- CONFIGURATION SÉCURISÉE ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLIENT_SECRET_JSON = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

# --- LOGS DE DÉMARRAGE CLAIRS ---
print("=" * 60)
print("🤖 Démarrage du bot Telegram (Mode Intelligent AF/AFR)")
print("=" * 60)

if not TELEGRAM_TOKEN:
    print("❌ ERREUR : TELEGRAM_TOKEN manquant dans l'environnement !")
else:
    print(f"✅ TELEGRAM_TOKEN présent (longueur={len(TELEGRAM_TOKEN)})")

if not RENDER_EXTERNAL_URL or RENDER_EXTERNAL_URL.startswith("http://localhost"):
    print(f"⚠️  ATTENTION : RENDER_EXTERNAL_URL non défini en prod → {RENDER_EXTERNAL_URL}")
else:
    print(f"✅ RENDER_EXTERNAL_URL = {RENDER_EXTERNAL_URL}")

if not CLIENT_SECRET_JSON:
    print("⚠️  ATTENTION : GOOGLE_CLIENT_SECRET_JSON manquant → OAuth Google désactivé.")
else:
    print("✅ GOOGLE_CLIENT_SECRET_JSON présent.")

# --- SUPPRESSION DU WEBHOOK ---
def supprimer_webhook_telegram():
    if not TELEGRAM_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook"
        r = requests.get(url, timeout=10)
        print(f"🧹 deleteWebhook → {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️  Erreur deleteWebhook : {e}")

# Initialisation de Flask
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
    c.execute('''
        CREATE TABLE IF NOT EXISTS suivi_vols (
            pilote_chat_id INTEGER PRIMARY KEY,
            numero_vol TEXT,
            dernier_etat TEXT
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
        redirect_uri=redirect_uri,
        autogenerate_code_verifier=False
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
            redirect_uri=redirect_uri,
            autogenerate_code_verifier=False
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
            f"Partagez-leur ce code pour qu'ils s'abonnent !",
            markup=generer_clavier_principal(is_pilote=True)
        )

        return "<h1>Connexion réussie !</h1><p>Vous pouvez fermer cette fenêtre et retourner sur Telegram.</p>"
    except Exception as e:
        return f"Erreur lors de la validation OAuth : {e}", 500

# ---------------------------------------------------------------------------
# LOGIQUE CLAVIER & BOT TELEGRAM
# ---------------------------------------------------------------------------
def generer_clavier_principal(is_pilote=False, is_proche=False):
    keyboard = []
    if is_pilote:
        keyboard.append([{"text": "✈️ Prochain Vol"}, {"text": "🔑 Mon Code Invité"}])
        keyboard.append([{"text": "🚪 Déconnexion Pilote"}])
    elif is_proche:
        keyboard.append([{"text": "✈️ Prochain Vol"}])
        keyboard.append([{"text": "🚪 Quitter le Pilote"}])
    else:
        keyboard.append([{"text": "👨‍✈️ Enregistrer mon Agenda (Pilote)"}])
        keyboard.append([{"text": "ℹ️ Aide"}])

    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "is_persistent": True
    }

def envoyer_message_telegram(chat_id, texte, parse_mode="Markdown", markup=None):
    if not TELEGRAM_TOKEN:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texte,
        "disable_web_page_preview": False
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if markup:
        payload["reply_markup"] = markup

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            return True

        if parse_mode and r.status_code == 400 and "can't parse" in r.text.lower():
            payload_plain = {k: v for k, v in payload.items() if k != "parse_mode"}
            r2 = requests.post(url, json=payload_plain, timeout=10)
            if r2.ok:
                return True
        return False
    except Exception as e:
        print(f"❌ Exception envoi Telegram : {e}")
        return False


def extraire_numero_vol(titre_event):
    match_vol = re.search(r'\b([A-Z0-9]{2}\s?\d{1,4}[A-Z]*)\b', titre_event, re.IGNORECASE)
    if match_vol:
        return match_vol.group(1).replace(" ", "").upper()
        
    match_immat = re.search(r'\b([A-Z]{1,2}-[A-Z0-9]{3,5})\b', titre_event, re.IGNORECASE)
    if match_immat:
        return match_immat.group(1).upper()
        
    return None


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
            maxResults=20, singleEvents=True,
            orderBy='startTime').execute()
        events = events_result.get('items', [])

        for event in events:
            titre = event.get('summary', '')
            numero_vol = extraire_numero_vol(titre)

            if numero_vol:
                debut_str = event['start'].get('dateTime', event['start'].get('date'))
                debut_dt = datetime.datetime.fromisoformat(debut_str)
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
    
    # Générer des variantes de recherche intelligentes (ex: AF344 -> AFR344, AFR344P, etc.)
    candidats = [numero_vol.strip().upper()]
    if numero_vol.upper().startswith("AF"):
        candidats.append(numero_vol.upper().replace("AF", "AFR", 1))
        
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            states = response.json().get("states", [])
            print(f"🔍 [OpenSky] {len(states)} aéronefs reçus. Recherche parmi les candidats : {candidats}")

            for state in states:
                callsign = state[1]
                if callsign:
                    callsign_clean = callsign.strip().upper()
                    for cand in candidats:
                        # Si le callsign contient le candidat (ex: AFR344P contient AFR344 ou AF344)
                        if cand in callsign_clean:
                            on_ground = state[8]
                            alt_ft = int(state[7] * 3.28084) if state[7] is not None else 0
                            print(f"🎯 [TROUVÉ] Vol {numero_vol} matché avec le callsign OpenSky : '{callsign_clean}' | Au sol: {on_ground}")
                            return {
                                "etat_brut": on_ground,
                                "etat": "Au sol 🟢" if on_ground else "En vol ✈️",
                                "altitude": f"{alt_ft} ft"
                            }
            print(f"❌ [OpenSky] Aucun candidat {candidats} trouvé dans la liste globale.")
    except Exception as e:
        print(f"⚠️ Erreur requête OpenSky : {e}")
    return None


def verifier_statut_utilisateur(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT chat_id FROM pilotes WHERE chat_id = ?', (chat_id,))
    is_pilote = c.fetchone() is not None
    
    c.execute('SELECT chat_id FROM proches WHERE chat_id = ?', (chat_id,))
    is_proche = c.fetchone() is not None
    conn.close()
    return is_pilote, is_proche


# ---------------------------------------------------------------------------
# TÂCHE DE FOND AUTOMATIQUE
# ---------------------------------------------------------------------------
def background_flight_monitor():
    print("🛰️ Thread de surveillance automatique des vols démarré.")
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT chat_id FROM pilotes')
            pilotes = c.fetchall()
            conn.close()

            for (pilote_chat_id,) in pilotes:
                vol = obtenir_prochain_vol_pour_pilote(pilote_chat_id)
                if not vol:
                    continue

                maintenant = datetime.datetime.now(datetime.timezone.utc)
                debut_dt = vol['debut_dt']
                if debut_dt.tzinfo is None:
                    debut_dt = debut_dt.replace(tzinfo=datetime.timezone.utc)

                delta_heures = (debut_dt - maintenant).total_seconds() / 3600

                if -12 <= delta_heures <= 12:
                    radar = verifier_ads_b(vol['numero'])
                    if not radar:
                        continue

                    etat_actuel = "AU_SOL" if radar['etat_brut'] else "EN_VOL"

                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    c.execute('SELECT numero_vol, dernier_etat FROM suivi_vols WHERE pilote_chat_id = ?', (pilote_chat_id,))
                    row_suivi = c.fetchone()
                    
                    ancien_vol = row_suivi[0] if row_suivi else None
                    ancien_etat = row_suivi[1] if row_suivi else None

                    if ancien_vol != vol['numero'] or ancien_etat != etat_actuel or ancien_etat is None:
                        print(f"📡 Changement détecté pour {vol['numero']} : {ancien_etat} ➔ {etat_actuel}")

                        c.execute('''
                            INSERT INTO suivi_vols (pilote_chat_id, numero_vol, dernier_etat)
                            VALUES (?, ?, ?)
                            ON CONFLICT(pilote_chat_id) DO UPDATE SET numero_vol = excluded.numero_vol, dernier_etat = excluded.dernier_etat
                        ''', (pilote_chat_id, vol['numero'], etat_actuel))
                        conn.commit()

                        if ancien_etat == "AU_SOL" and etat_actuel == "EN_VOL":
                            texte_alerte = f"🛫 **ALERTE DÉCOLLAGE !**\n\nLe vol `{vol['numero']}` ({vol['titre']}) vient de décoller !\nAltitude : {radar['altitude']}"
                        elif ancien_etat == "EN_VOL" and etat_actuel == "AU_SOL":
                            texte_alerte = f"🛬 **ALERTE ATTERRISSAGE !**\n\nLe vol `{vol['numero']}` ({vol['titre']}) vient d'atterrir."
                        else:
                            if etat_actuel == "EN_VOL":
                                texte_alerte = f"🛫 **ALERTE EN VOL !**\n\nLe vol `{vol['numero']}` ({vol['titre']}) est détecté en vol !\nAltitude : {radar['altitude']}"
                            else:
                                texte_alerte = f"✈️ **Point sur le vol :**\n\nLe vol `{vol['numero']}` ({vol['titre']}) est actuellement **{radar['etat']}**."

                        destinataires = [pilote_chat_id]
                        c.execute('SELECT chat_id FROM proches WHERE pilote_chat_id = ?', (pilote_chat_id,))
                        proches = c.fetchall()
                        for (proche_chat_id,) in proches:
                            destinataires.append(proche_chat_id)
                        
                        conn.close()

                        link_fr24 = f"https://www.flightradar24.com/data/flights/{vol['numero']}"
                        message_final = f"{texte_alerte}\n\n🔗 [Suivre sur Flightradar24]({link_fr24})"

                        for dest_id in destinataires:
                            envoyer_message_telegram(dest_id, message_final)
                    else:
                        conn.close()

        except Exception as e:
            print(f"❌ Erreur dans le thread de surveillance : {e}")

        time.sleep(120)


# ---------------------------------------------------------------------------
# LOGIQUE BOT TELEGRAM (POLLING)
# ---------------------------------------------------------------------------
def lancer_bot_interactif():
    print("🤖 Bot Telegram Multi-utilisateurs démarré (mode polling)...")
    supprimer_webhook_telegram()
    offset = 0

    while True:
        try:
            if not TELEGRAM_TOKEN:
                time.sleep(10)
                continue

            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={offset}&timeout=30"
            try:
                response = requests.get(url, timeout=35)
                data = response.json()
            except Exception:
                time.sleep(5)
                continue

            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                if "message" in update and "text" in update["message"]:
                    chat_id = update["message"]["chat"]["id"]
                    texte = update["message"]["text"].strip()

                    is_pilote, is_proche = verifier_statut_utilisateur(chat_id)

                    if texte.startswith("/start") or texte == "ℹ️ Aide":
                        msg = (
                            "👋 **Bienvenue sur le Bot de Suivi de Vol Automatique !**\n\n"
                            "Ce bot prévient automatiquement les proches lors des décollages et atterrissages.\n"
                            "Utilisez les boutons ci-dessous :"
                        )
                        envoyer_message_telegram(chat_id, msg, markup=generer_clavier_principal(is_pilote, is_proche))

                    elif texte.startswith("/register") or texte == "👨‍✈️ Enregistrer mon Agenda (Pilote)":
                        link = f"{RENDER_EXTERNAL_URL}/login?chat_id={chat_id}"
                        message_texte = (
                            "🔗 **Connexion Pilote Google Calendar :**\n\n"
                            f"Cliquez sur ce lien pour autoriser l'accès à votre agenda :\n{link}"
                        )
                        envoyer_message_telegram(chat_id, message_texte)

                    elif texte.startswith("/moncode") or texte == "🔑 Mon Code Invité":
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute('SELECT code_invitation FROM pilotes WHERE chat_id = ?', (chat_id,))
                        row = c.fetchone()
                        conn.close()

                        if row and row[0]:
                            envoyer_message_telegram(chat_id, f"🔑 Votre code d'invitation proche est : `{row[0]}`")
                        else:
                            envoyer_message_telegram(chat_id, "❌ Vous n'êtes pas enregistré comme pilote.")

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
                            
                            envoyer_message_telegram(
                                chat_id, 
                                "✅ Vous êtes rattaché au pilote ! Vous recevrez désormais les alertes automatiques de vol.", 
                                markup=generer_clavier_principal(is_pilote=False, is_proche=True)
                            )
                        else:
                            conn.close()
                            envoyer_message_telegram(chat_id, "❌ Code d'invitation invalide.")

                    elif texte.startswith("/vol") or texte == "✈️ Prochain Vol":
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
                                "Veuillez enregistrer votre agenda de pilote ou utiliser `/join CODE` pour suivre un pilote."
                            )
                            continue

                        envoyer_message_telegram(chat_id, "🔍 Recherche du prochain vol filtré...")
                        vol = obtenir_prochain_vol_pour_pilote(pilote_target_id)

                        if not vol:
                            envoyer_message_telegram(chat_id, "❌ Aucun vol valide trouvé dans l'agenda.")
                        else:
                            live = verifier_ads_b(vol['numero'])
                            statut_live = f"État : {live['etat']} | Altitude : {live['altitude']}" if live else "Avion non détecté sur le radar."
                            link_fr24 = f"https://www.flightradar24.com/data/flights/{vol['numero']}"

                            reponse = (
                                f"✈️ **Prochain vol trouvé :**\n\n"
                                f"• **Titre :** {vol['titre']}\n"
                                f"• **Vol identifié :** `{vol['numero']}`\n"
                                f"• **Départ prévu :** {vol['debut_dt'].strftime('%d/%m/%Y à %H:%M')}\n\n"
                                f"📡 **Statut radar :**\n{statut_live}\n\n"
                                f"🔗 [Suivre sur Flightradar24]({link_fr24})"
                            )
                            envoyer_message_telegram(chat_id, reponse)

                    elif texte in ["🚪 Déconnexion Pilote", "🚪 Quitter le Pilote", "/logout"]:
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute('DELETE FROM pilotes WHERE chat_id = ?', (chat_id,))
                        c.execute('DELETE FROM proches WHERE chat_id = ?', (chat_id,))
                        c.execute('DELETE FROM suivi_vols WHERE pilote_chat_id = ?', (chat_id,))
                        conn.commit()
                        conn.close()

                        envoyer_message_telegram(
                            chat_id,
                            "🔒 **Déconnexion effectuée.** Vos données et abonnements ont été supprimés.",
                            markup=generer_clavier_principal(is_pilote=False, is_proche=False)
                        )

        except Exception as e:
            print(f"❌ Erreur boucle bot : {e}")
            time.sleep(5)
        time.sleep(2)


# Lancement global propre
if __name__ == '__main__':
    threading.Thread(target=lancer_bot_interactif, daemon=True).start()
    threading.Thread(target=background_flight_monitor, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)