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

# Pour stocker les derniers états connus des vols et éviter de spamer les notifs
DERNIERS_ETATS_VOLS = {}
# Liste des chat_id abonnés aux alertes automatiques (par défaut, tous ceux qui ont parlé au bot)
CHATS_ABONNES = set()

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

def diffuser_a_tous(texte):
    """Envoie une notification automatique à tous les utilisateurs abonnés"""
    for chat_id in CHATS_ABONNES:
        envoyer_message_telegram(chat_id, texte)

def extraire_aeroport_arrivee(titre_event):
    """
    Extrait l'aéroport de destination si le format est du type CDG-MME ou similaire.
    """
    match = re.search(r'[A-Z]{3}-([A-Z]{3})', titre_event)
    if match:
        return match.group(1)
    return None

def obtenir_meteo_aeroport(code_iata):
    """
    Récupère la météo actuelle gratuite via wttr.in pour l'aéroport d'arrivée
    """
    try:
        url = f"https://wttr.in/{code_iata}?format=%C+%t+(Vent:+%w)"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return response.text.strip()
    except Exception:
        pass
    return "Météo indisponible"

def extraire_numero_vol(titre_event):
    match = re.search(r'\b([A-Z0-9]{2,3}\s?\d{1,4})\b', titre_event, re.IGNORECASE)
    if match:
        return match.group(1).replace(" ", "").upper()
    return None

def obtenir_prochain_vol():
    if not GOOGLE_TOKEN_JSON:
        return None

    try:
        token_data = json.loads(GOOGLE_TOKEN_JSON)
        with open('token.json', 'w') as f:
            json.dump(token_data, f)
    except Exception as e:
        print(f"❌ Erreur token Google : {e}")
        return None

    if not os.path.exists('token.json'):
        return None

    try:
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        service = build('calendar', 'v3', credentials=creds)
        now = datetime.datetime.utcnow().isoformat() + 'Z'

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

            if numero_vol or "VOL" in titre.upper() or re.search(r'\b[A-Z]{3}-[A-Z]{3}\b', titre):
                debut_str = event['start'].get('dateTime', event['start'].get('date'))
                debut_dt = datetime.datetime.fromisoformat(debut_str)

                if not numero_vol:
                    match_fallback = re.search(r'^([A-Z0-9]+)', titre)
                    numero_vol = match_fallback.group(1) if match_fallback else titre

                 aeroport_arrivee = extraire_aeroport_arrivee(titre)
                 meteo = obtenir_meteo_aeroport(aeroport_arrivee) if aeroport_arrivee else "N/A"

                return {
                    "numero": numero_vol,
                    "titre": titre,
                    "debut_dt": debut_dt,
                    "arrivee": aeroport_arrivee,
                    "meteo": meteo
                }
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

def surveille_vols_arriere_plan():
    """Tâche de fond qui vérifie l'état du vol toutes les 5 minutes pour envoyer des alertes"""
    while True:
        try:
            time.sleep(300) # Vérification toutes les 5 minutes
            if not CHATS_ABONNES:
                continue

            vol = obtenir_prochain_vol()
            if vol:
                num = vol['numero']
                live = verifier_ads_b(num)
                if live:
                    nouvel_etat = live['etat']
                    ancien_etat = DERNIERS_ETATS_VOLS.get(num)

                    # Si l'état a changé (ex: passe de Au sol à En vol, ou inversement)
                    if ancien_etat and ancien_etat != nouvel_etat:
                        alerte = (
                            f"🔔 **Changement de statut pour le vol {vol['titre']} !**\n\n"
                            f"L'avion est maintenant : **{nouvel_etat}**\n"
                            f"Altitude : {live['altitude']}"
                        )
                        diffuser_a_tous(alerte)

                    DERNIERS_ETATS_VOLS[num] = nouvel_etat
        except Exception as e:
            print(f"Erreur tâche de fond : {e}")

# Lancement de la surveillance en arrière-plan
threading.Thread(target=surveille_vols_arriere_plan, daemon=True).start()

def message_accueil(chat_id):
    texte = (
        "👋 **Bienvenue sur le bot 'Où est Rafael ?'** ✈️\n\n"
        "Ce bot permet à ses proches et collègues de suivre en temps réel les prochains vols de Rafael !\n\n"
        "📌 **Commandes disponibles :**\n"
        "• `/vol` : Connaître le prochain vol prévu, la météo à l'arrivée et le statut en direct.\n"
        "• `/aide` : Obtenir un rappel du fonctionnement du bot.\n\n"
        "🔔 Vous êtes désormais abonné aux alertes automatiques en cas de changement de statut en vol !"
    )
    envoyer_message_telegram(chat_id, texte)

def lancer_bot_interactif():
    print("🤖 Bot 'Où est Rafael ?' prêt avec options avancées...")
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
                        CHATS_ABONNES.add(chat_id) # Inscription automatique aux alertes
                        texte_recu = update["message"]["text"].strip()

                        if texte_recu.startswith("/start"):
                            message_accueil(chat_id)

                        elif texte_recu.startswith("/vol"):
                            envoyer_message_telegram(chat_id, "🔍 Recherche du vol de Rafael et de la météo...")

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
                                    f"• **Départ prévu :** {vol['debut_dt'].strftime('%d/%m/%Y à %H:%M')}\n"
                                    f"• **Météo à l'arrivée ({vol['arrivee']}) :** {vol['meteo']}\n\n"
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