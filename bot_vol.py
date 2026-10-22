import datetime
import os
import re
import json
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# --- CONFIGURATION VIA VARIABLES D'ENVIRONNEMENT ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON")

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

def envoyer_message_telegram(texte):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ Tokens Telegram manquants.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": texte,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("✅ Notification Telegram envoyée !")
        else:
            print(f"❌ Erreur Telegram : {response.text}")
    except Exception as e:
        print(f"Erreur réseau Telegram : {e}")

def obtenir_prochain_vol():
    if not GOOGLE_TOKEN_JSON:
        print("⚠️ Le token Google est manquant dans les variables d'environnement.")
        return None

    # Recrée temporairement le fichier token.json à partir du secret GitHub
    with open('token.json', 'w') as f:
        f.write(GOOGLE_TOKEN_JSON)

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
                return {
                    "on_ground": on_ground,
                    "altitude": alt_ft
                }
    except Exception:
        pass
    return None

def main():
    print("🔍 Vérification du planning de vol...")
    vol = obtenir_prochain_vol()

    if not vol:
        print("Aucun vol à venir trouvé.")
        return

    print(f"Prochain vol : {vol['titre']} prévu à {vol['debut_dt']}")

    maintenant = datetime.datetime.now(vol['debut_dt'].tzinfo)
    delta_minutes = (vol['debut_dt'] - maintenant).total_seconds() / 60

    # Si on est entre 60 minutes avant le vol et la fin estimée
    if -180 <= delta_minutes <= 60:
        live = verifier_ads_b(vol['numero'])
        if live:
            print(f"Statut live -> Au sol: {live['on_ground']}, Altitude: {live['altitude']} ft")
            # Ici, tu pourrais affiner l'envoi en fonction d'un fichier d'état stocké sur GitHub si besoin,
            # mais pour un premier test, voyons déjà si le script s'exécute bien toutes les 5 minutes !

if __name__ == '__main__':
    main()