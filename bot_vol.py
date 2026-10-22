import datetime
import os
import re
import time
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# --- CONFIGURATION ---
TELEGRAM_TOKEN = "8560528662:AAHX-LMw6OUvnnBdP42gAc90arhQBI-nm9E"

# Ton token Google stocké en dur pour que le bot sache toujours lire ton agenda
GOOGLE_TOKEN_DATA = {
    "token": "ya29.a0AX07Cmv3qqR_0RfsE6vow2-l9KOBBzIKf57H8CmdzgapqauXImvhPn4ZqCTKdniKYdeIwrfYXAVuu_RUghy3qReeUZKiuHEO-YZZr-9Ls4Wn6Xgox5UqssOD5BAI8wPKqA1p-xN4NcefU_8udBZj760HNCqvcRaUklV6zFzvS-ibIYTTpdypMX0TZSQWe3PTr4xoBhMaCgYKAc0SARESFQHGX2MilmrWbHGT8wByHPNu5INvQw0206",
    "refresh_token": "1//03K_5SSgbWpJlCgYIARAAGAMSNwF-L9IrKLGmiHi4Vm7YKNUo0QWMGFZSSyN7qIrpppSAspRmRqUa8du1iQvjcyH-L4QbDKqYcRI",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "765453215343-9uqs52ibfac4686tg0b4ofnuf4dt1ga7.apps.googleusercontent.com",
    "client_secret": "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",
    "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
    "universe_domain": "googleapis.com",
    "account": "",
    "expiry": "2026-10-22T08:40:48Z"
}

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

def envoyer_message_telegram(chat_id, texte):
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
    import json
    with open('token.json', 'w') as f:
        json.dump(GOOGLE_TOKEN_DATA, f)

    if not os.path.exists('token.json'):
        print("⚠️ Fichier token.json introuvable.")
        return None

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