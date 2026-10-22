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

threading.Thread(target=demarrer_serveur_web, daemon=True).start()


def envoyer_message_telegram(chat_id, texte):
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN manquant !")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texte,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"❌ Erreur Telegram : {response.text}")
    except Exception as e:
        print(f"Erreur réseau Telegram : {e}")

def extraire_numero_vol(titre_event):
    """
    Détecte si un événement contient un vrai numéro de vol (IATA/OACI).
    Exemples détectés : 3V4717, AF123, TO4567, HV612, etc.
    """
    # Motif strict pour numéro de vol : 2 à 3 caractères alpanumériques suivis de 1 à 4 chiffres
    match = re.search(r'\b([A-Z0-9]{2,3}\s?\d{1,4})\b', titre_event, re.IGNORECASE)
    if match:
        return match.group(1).replace(" ", "").upper()
    return None

def obtenir_prochain_vol():
    if not GOOGLE_TOKEN_JSON:
        print("⚠️ GOOGLE_TOKEN_JSON manquant dans les variables d'environnement.")
        return None

    try:
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

        # On récupère jusqu'à 10 événements à venir pour chercher un vrai vol
        events_result = service.events().list(
            calendarId='primary', timeMin=now,
            maxResults=10, singleEvents=True,
            orderBy='startTime').execute()
        events = events_result.get('items', [])

        if not events:
            return None

        for event in events:
            titre = event.get('summary', '')
            numero_vol = extraire_numero_vol(titre)

            # Si le titre contient un motif de vol ou des mots-clés typiques de vol/eCrew
            if numero_vol or "VOL" in titre.upper() or re.search(r'\b[A-Z]{3}-[A-Z]{3}\b', titre):
                debut_str = event['start'].get('dateTime', event['start'].get('date'))
                debut_dt = datetime.datetime.fromisoformat(debut_str)

                # Si le numéro de vol n'a pas été capturé par le regex, on prend le premier mot
                if not numero_vol:
                    match_fallback = re.search(r'^([A-Z0-9]+)', titre)
                    numero_vol = match_fallback.group(1) if match_fallback else titre

                return {
                    "numero": numero_vol,
                    "titre": titre,
                    "debut_dt": debut_dt
                }

        # Aucun événement correspondant à un vol trouvé
        return None

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
            if callsign and numero_vol.strip().upper() in callsign.strip().upper():
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

def message_accueil(chat_id):
    texte = (
        "👋 **Bienvenue sur le bot 'Où est Rafael ?'** ✈️\n\n"
        "Ce bot permet à ses proches et collègues de suivre en temps réel les prochains vols et l'activité de Rafael !\n\n"
        "📌 **Commandes disponibles :**\n"
        "• `/vol` : Connaître le prochain vol prévu et son statut de suivi en direct.\n"
        "• `/aide` : Obtenir un rappel du fonctionnement du bot.\n\n"
        "📡 **Comment ça marche ?**\n"
        "1️⃣ Le bot consulte le planning de Rafael en direct via Google Calendar.\n"
        "2️⃣ Il filtre intelligemment pour ne retenir que les vrais vols.\n"
        "3️⃣ Si le vol est en cours, il interroge les radars ADSB (OpenSky) pour vous donner l'altitude et le statut en direct !\n\n"
        "Tapez `/vol` dès maintenant pour tester !"
    )
    envoyer_message_telegram(chat_id, texte)

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

                        if texte_recu.startswith("/start"):
                            message_accueil(chat_id)

                        elif texte_recu.startswith("/vol"):
                            envoyer_message_telegram(chat_id, "🔍 Recherche du vol de Rafael dans l'agenda...")

                            vol = obtenir_prochain_vol()
                            if not vol:
                                envoyer_message_telegram(chat_id, "❌ Aucun vol à venir trouvé dans l'agenda.")
                            else:
                                live = verifier_ads_b(vol['numero'])
                                statut_live = f"État : {live['etat']} | Altitude : {live['altitude']}" if live else "Avion non détecté sur le radar pour le moment."

                                link_fr24 = f"https://www.flightradar24.com/data/flights/{vol['numero']}"

                                reponse = (
                                    f"✈️ **Prochain vol de Rafael :**\n\n"
                                    f"• **Vol :** {vol['titre']}\n"
                                    f"• **Départ prévu :** {vol['debut_dt'].strftime('%d/%m/%Y à %H:%M')}\n\n"
                                    f"📡 **Statut radar en direct :**\n{statut_live}\n\n"
                                    f"🔗 [Suivre le vol sur Flightradar24]({link_fr24})"
                                )
                                envoyer_message_telegram(chat_id, reponse)

                        elif texte_recu.startswith("/aide"):
                            message_accueil(chat_id)

        except Exception as e:
            print(f"❌ Erreur : {e}")
            time.sleep(5)

        time.sleep(2)

if __name__ == '__main__':
    lancer_bot_interactif()