import datetime
import os
import re
import json
import time
import requests
import http.server
import socketserver
import threading
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# --- CONFIGURATION SÉCURISÉE ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON")

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

# 1. PETIT SERVEUR HTTP POUR RENDER & UPTIMEROBOT
# Render a besoin qu'un port soit ouvert, et UptimeRobot viendra le pinger ici.
PORT = int(os.environ.get("PORT", 10000))

class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot 'Ou est Rafael ?' is alive and running!")

def demarrer_serveur_web():
    with socketserver.TCPServer(("", PORT), HealthCheckHandler) as httpd:
        print(f"🌐 Serveur web de santé actif sur le port {PORT}")
        httpd.serve_forever()

# Lancement du serveur web en arrière-plan (thread)
threading.Thread(target=demarrer_serveur_web, daemon=True).start()


def envoyer_message_telegram(chat_id, texte):
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN manquant !")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texte,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"❌ Erreur Telegram : {response.text}")
    except Exception as e:
        print(f"Erreur réseau Telegram : {e}")

def obtenir_prochain_vol():
    if not GOOGLE_TOKEN_JSON:
        print("⚠️ GOOGLE_TOKEN_JSON manquant dans les variables d'environnement.")
        return None

    try:
        # Reconstruction du token.json à partir de la variable d'environnement
        token_data = json.loads(GOOGLE_TOKEN_JSON)
        with open('token.json', 'w') as f:
            json.dump(token_data, f)
    except Exception as e:
        print(f"❌ Erreur lors de l'écriture du token Google : {e}")
        return None

    if not os.path.exists('token.json'):
        return None

    try:
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        service = build('calendar', 'v3', credentials=creds)
        now = datetime.datetime.utcnow().isoformat() + 'Z'

        events_result = service.events().list(
            calendarId='primary', timeMin=now,
            maxResults=1, singleEvents=True,
            orderBy='startTime').execute()
        events = events_result.get('items', [])

        if not events:
            return None

        event = events[0]
        titre_vol = event['summary']
        debut_str = event['start'].get('dateTime', event['start'].get('date'))
        debut_dt = datetime.datetime.fromisoformat(debut_str)

        match = re.search(r'^([A-Z0-9]+)', titre_vol)
        numero_vol = match.group(1) if match else titre_vol

        return {
            "numero": numero_vol,
            "titre": titre_vol,
            "debut_dt": debut_dt
        }
    except Exception as e:
        print(f"Erreur Google Calendar : {e}")
        return None

def verifier_ads_b(numero_vol):
    url = "https://opensky-network.org/api/states/all"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return None

        data = response.json()
        states = data.get("states", [])

        for state in states:
            callsign = state[1]
            if callsign and numero_vol.strip() in callsign.strip():
                on_ground = state[8]
                altitude_m = state[7]
                alt_ft = int(altitude_m * 3.28084) if altitude_m is not None else 0
                etat = "Au sol 🟢" if on_ground else "En vol ✈️"
                return {
                    "etat": etat,
                    "altitude": f"{alt_ft} ft"
                }
    except Exception:
        pass
    return None

def lancer_bot_interactif():
    print("🤖 Bot 'Où est Rafael ?' prêt pour les proches en écoute...")
    offset = 0

    while True:
        try:
            if not TELEGRAM_TOKEN:
                print("⚠️ En attente du TELEGRAM_TOKEN...")
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
                        texte_recu = update["message"]["text"].strip()

                        print(f"📩 Requête reçue de {chat_id} : {texte_recu}")

                        if texte_recu.startswith("/vol") or texte_recu.startswith("/start"):
                            envoyer_message_telegram(chat_id, "🔍 Recherche du vol de Rafael dans l'agenda...")

                            vol = obtenir_prochain_vol()
                            if not vol:
                                envoyer_message_telegram(chat_id, "❌ Aucun vol à venir trouvé dans l'agenda.")
                            else:
                                live = verifier_ads_b(vol['numero'])
                                statut_live = f"État : {live['etat']} | Altitude : {live['altitude']}" if live else "Avion non détecté en vol pour l'instant."

                                reponse = (
                                    f"✈️ **Prochain vol de Rafael :**\n\n"
                                    f"• **Vol :** {vol['titre']}\n"
                                    f"• **Prévu le :** {vol['debut_dt'].strftime('%Y-%m-%d à %H:%M')}\n\n"
                                    f"📡 **Statut en direct :**\n{statut_live}"
                                )
                                envoyer_message_telegram(chat_id, reponse)

                        elif texte_recu.startswith("/aide"):
                            envoyer_message_telegram(chat_id, "ℹ️ **Bot 'Où est Rafael ?'**\nTape /vol pour connaître le prochain vol de Rafael et son statut en direct.")

        except Exception as e:
            print(f"❌ Erreur : {e}")
            time.sleep(5)

        time.sleep(2)

if __name__ == '__main__':
    lancer_bot_interactif()