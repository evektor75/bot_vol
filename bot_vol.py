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
from flask import Flask, request, redirect
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLIENT_SECRET_JSON = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lstrip("@").strip()
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
DB_FILE = os.environ.get("BOT_DB_FILE", "bot_vols.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
NOTIFICATION_INTERVAL_SECONDS = int(os.environ.get("NOTIFICATION_INTERVAL_SECONDS", "60"))
NOTIFICATION_REMINDER_MINUTES = int(os.environ.get("NOTIFICATION_REMINDER_MINUTES", "120"))
CACHE_OPENSKY_TTL = int(os.environ.get("OPENSKY_CACHE_TTL", "45"))
CACHE_CALENDAR_TTL = int(os.environ.get("CALENDAR_CACHE_TTL", "300"))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot_vol")
pending_verifiers = {}

TRANSLATIONS = {
    "fr": {
        "welcome": "👋 Bienvenue sur le Bot de Suivi de Vol Multi-Pilotes !\n\n👨‍✈️ Pilote ?\n• /register pour lier votre Google Calendar.\n• /moncode pour voir votre code d’invitation.\n• /invite pour obtenir un lien à partager.\n\n👪 Proche/Famille ?\n• Cliquez sur un lien d’invitation ou utilisez /join CODE.\n• /vol pour consulter le prochain vol.\n\n🌐 Langue : /lang fr ou /lang en",
        "help": "Commandes :\n/register — connecter Google Calendar (pilote)\n/moncode — afficher le code d’invitation (pilote)\n/invite — créer le lien d’invitation (pilote)\n/join CODE — s’abonner avec un code\n/vol — consulter le prochain vol\n/status — afficher l’état\n/reconnect — reconnecter Google\n/disconnect — déconnecter Google\n/lang fr|en — changer de langue\n/stop — se désabonner (proche)\n/help — afficher cette aide",
        "register_link": "🔗 Connexion Pilote Google Calendar :\n\nCliquez sur ce lien pour autoriser l’accès à votre agenda :\n{link}", "register_error": "❌ Erreur pendant /register : {error}", "oauth_success": "✅ Compte Google connecté avec succès !\n\nVotre code d’invitation pour vos proches est : {code}\nPartagez-leur ce lien :\n{invite}", "oauth_page": "Connexion réussie ! Vous pouvez fermer cette fenêtre et retourner sur Telegram.", "oauth_fail": "Échec de l’authentification.", "lang_usage": "🌐 Utilisation : /lang fr ou /lang en", "lang_changed": "✅ Langue enregistrée : français.", "lang_changed_en": "✅ Language saved: English.", "code": "🔑 Votre code d’invitation proche est : {code}", "not_registered": "❌ Vous n’êtes pas encore enregistré. Tapez /register d’abord.", "invite": "🔗 Lien à partager avec votre proche :\n{link}", "invite_missing": "❌ Vous devez connecter Google Calendar avec /register avant de créer un lien.", "join_usage": "⚠️ Format incorrect. Utilisez /join CODE.", "joined": "✅ Vous êtes rattaché au pilote ! Tapez /vol pour voir les vols.", "invalid_code": "❌ Code d’invitation invalide.", "stopped": "✅ Vous êtes désabonné.", "not_subscribed": "ℹ️ Vous n’êtes abonné à aucun pilote.", "access": "🔒 Accès restreint. Entrez un code avec /join CODE.", "searching": "🔍 Recherche du prochain vol…", "no_flight": "❌ Aucun vol trouvé dans l’agenda.", "flight": "✈️ Prochain vol :\n\n• Vol : {title}\n• Départ prévu : {date}\n\n📡 Statut radar :\n{status}\n\n🔗 Suivre sur Flightradar24 :\n{link}", "radar_ground": "Au sol 🟢 | Altitude : {altitude}", "radar_air": "En vol ✈️ | Altitude : {altitude}", "radar_missing": "Avion non détecté sur le radar.", "invite_config": "⚠️ BOT_USERNAME n’est pas configuré. Utilisez le code /moncode.", "menu_flights": "Vol en cours", "menu_status": "Statut", "menu_invite": "Inviter", "menu_lang": "Langue FR/EN", "menu_reconnect": "Reconnecter Google", "menu_disconnect": "Déconnecter", "status": "📊 Statut\nLangue : {lang}\nGoogle : {google}\nProches abonnés : {contacts}\nProchain duty : {duty}\nNotifications : {notifications}", "connected": "connecté", "expired": "expiré", "not_connected": "non connecté", "none": "aucun", "enabled": "actives", "disabled": "inactives", "disconnect_confirm": "Voulez-vous révoquer et supprimer vos identifiants Google ?", "disconnect_done": "✅ Google Calendar déconnecté.", "disconnect_cancel": "Déconnexion annulée.", "reconnect_hint": "🔗 Reconnectez Google avec /register.", "calendar_expired": "⚠️ Votre connexion Google a expiré. Reconnectez-la pour recevoir les duties.", "notif_departure": "⏰ Rappel : vol {flight} ({iata}) prévu à {time}.", "notif_takeoff": "🛫 Vol {flight} ({iata}) décollé. ETA : {eta}.", "notif_landing": "🛬 Vol {flight} ({iata}) atterri.", "callback_unknown": "Action inconnue."},
    "en": {
        "welcome": "👋 Welcome to the Multi-Pilot Flight Tracker!\n\n👨‍✈️ Pilot?\n• /register to link your Google Calendar.\n• /moncode to see your invitation code.\n• /invite to get a shareable link.\n\n👪 Family/Contact?\n• Click an invitation link or use /join CODE.\n• /vol to view the next flight.\n\n🌐 Language: /lang fr or /lang en", "help": "Commands:\n/register — connect Google Calendar\n/moncode — show invitation code\n/invite — create an invitation link\n/join CODE — subscribe with a code\n/vol — view the next flight\n/status — show status\n/reconnect — reconnect Google\n/disconnect — disconnect Google\n/lang fr|en — change language\n/stop — unsubscribe\n/help — show this help", "register_link": "🔗 Pilot Google Calendar connection:\n\nClick this link to authorize your calendar:\n{link}", "register_error": "❌ Error during /register: {error}", "oauth_success": "✅ Google account connected successfully!\n\nYour invitation code is: {code}\nShare this link:\n{invite}", "oauth_page": "Connection successful! You can close this window and return to Telegram.", "oauth_fail": "Authentication failed.", "lang_usage": "🌐 Usage: /lang fr or /lang en", "lang_changed": "✅ Language saved: French.", "lang_changed_en": "✅ Language saved: English.", "code": "🔑 Your invitation code is: {code}", "not_registered": "❌ You are not registered yet. Type /register first.", "invite": "🔗 Link to share with your contact:\n{link}", "invite_missing": "❌ Connect Google Calendar with /register first.", "join_usage": "⚠️ Incorrect format. Use /join CODE.", "joined": "✅ You are now linked to the pilot! Type /vol to view flights.", "invalid_code": "❌ Invalid invitation code.", "stopped": "✅ You are unsubscribed.", "not_subscribed": "ℹ️ You are not subscribed to any pilot.", "access": "🔒 Access restricted. Enter a code with /join CODE.", "searching": "🔍 Looking for the next flight…", "no_flight": "❌ No flight found in the calendar.", "flight": "✈️ Next flight:\n\n• Flight: {title}\n• Scheduled departure: {date}\n\n📡 Radar status:\n{status}\n\n🔗 Track on Flightradar24:\n{link}", "radar_ground": "On ground 🟢 | Altitude: {altitude}", "radar_air": "In flight ✈️ | Altitude: {altitude}", "radar_missing": "Aircraft not detected on radar.", "invite_config": "⚠️ BOT_USERNAME is not configured. Use /moncode.", "menu_flights": "Current flight", "menu_status": "Status", "menu_invite": "Invite", "menu_lang": "Language FR/EN", "menu_reconnect": "Reconnect Google", "menu_disconnect": "Disconnect", "status": "📊 Status\nLanguage: {lang}\nGoogle: {google}\nSubscribed contacts: {contacts}\nNext duty: {duty}\nNotifications: {notifications}", "connected": "connected", "expired": "expired", "not_connected": "not connected", "none": "none", "enabled": "enabled", "disabled": "disabled", "disconnect_confirm": "Revoke and delete your Google credentials?", "disconnect_done": "✅ Google Calendar disconnected.", "disconnect_cancel": "Disconnect cancelled.", "reconnect_hint": "🔗 Reconnect Google with /register.", "calendar_expired": "⚠️ Your Google connection expired. Reconnect to receive duties.", "notif_departure": "⏰ Reminder: flight {flight} ({iata}) scheduled at {time}.", "notif_takeoff": "🛫 Flight {flight} ({iata}) took off. ETA: {eta}.", "notif_landing": "🛬 Flight {flight} landed.", "callback_unknown": "Unknown action."}
}

def normalize_lang(value): return "fr" if str(value or "").lower().startswith("fr") else "en"
def t(key, lang="en", **kwargs): return TRANSLATIONS.get(normalize_lang(lang), TRANSLATIONS["en"]).get(key, key).format(**kwargs)

class _DBConnection:
    # SQLite fallback or one short-lived Neon connection.
    def __init__(self):
        self.is_postgres = bool(DATABASE_URL)
        self.conn = None
        self._connect()
    def _connect(self):
        if self.is_postgres:
            try:
                import psycopg2
                from psycopg2 import OperationalError
                self._operational_error = OperationalError
            except ImportError as exc:
                raise RuntimeError("DATABASE_URL est défini mais psycopg2-binary n'est pas installé") from exc
            last_error = None
            for attempt in range(3):
                try:
                    self.conn = psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
                    return
                except OperationalError as exc:
                    last_error = exc
                    if attempt < 2: time.sleep(0.5 * (attempt + 1))
            raise last_error
        self.conn = sqlite3.connect(DB_FILE, timeout=30)
    def _sql(self, sql):
        if not self.is_postgres: return sql
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        if "INSERT INTO notification_state VALUES" in sql and "ON CONFLICT" not in sql:
            sql += " ON CONFLICT (chat_id, event_id, notification_type) DO NOTHING"
        return sql.replace("?", "%s")
    def execute(self, sql, params=()):
        # psycopg2 cursor.execute() returns None; expose the cursor so callers
        # can immediately use fetchone(), fetchall(), iteration and rowcount
        # with either SQLite or Postgres.
        converted_sql = self._sql(sql)
        try:
            cur = self.conn.cursor()
            cur.execute(converted_sql, params)
            return cur
        except Exception as exc:
            if self.is_postgres:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                # A Neon connection can drop between two short-lived requests.
                # Reconnect once for connection-level failures, and return the
                # retried cursor just like the first attempt.
                if isinstance(exc, self._operational_error):
                    self._connect()
                    cur = self.conn.cursor()
                    cur.execute(converted_sql, params)
                    return cur
            raise
    def cursor(self): return self.conn.cursor()
    def commit(self): return self.conn.commit()
    def rollback(self): return self.conn.rollback()
    def close(self): return self.conn.close()
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type: self.rollback()
            else: self.commit()
        finally: self.close()

def db_connect(): return _DBConnection()

def init_db():
    with db_connect() as conn:
        if DATABASE_URL:
            conn.execute("CREATE TABLE IF NOT EXISTS pilotes (chat_id BIGINT PRIMARY KEY, nom TEXT, code_invitation TEXT UNIQUE, google_token TEXT, language TEXT DEFAULT 'en')")
            conn.execute("CREATE TABLE IF NOT EXISTS proches (chat_id BIGINT PRIMARY KEY, pilote_chat_id BIGINT, FOREIGN KEY (pilote_chat_id) REFERENCES pilotes (chat_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS notification_state (chat_id BIGINT, event_id TEXT, notification_type TEXT, sent_at TEXT, PRIMARY KEY(chat_id, event_id, notification_type))")
        else:
            conn.execute("CREATE TABLE IF NOT EXISTS pilotes (chat_id INTEGER PRIMARY KEY, nom TEXT, code_invitation TEXT UNIQUE, google_token TEXT, language TEXT DEFAULT 'en')")
            conn.execute('CREATE TABLE IF NOT EXISTS proches (chat_id INTEGER PRIMARY KEY, pilote_chat_id INTEGER, FOREIGN KEY (pilote_chat_id) REFERENCES pilotes (chat_id))')
            conn.execute('CREATE TABLE IF NOT EXISTS notification_state (chat_id INTEGER, event_id TEXT, notification_type TEXT, sent_at TEXT, PRIMARY KEY(chat_id,event_id,notification_type))')
            cols = {row[1] for row in conn.execute("PRAGMA table_info(pilotes)")}
            if "language" not in cols: conn.execute("ALTER TABLE pilotes ADD COLUMN language TEXT DEFAULT 'en'")

init_db()

def generer_code_invitation(): return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
def get_language(chat_id, fallback="en"):
    with db_connect() as c: row = c.execute('SELECT language FROM pilotes WHERE chat_id=?', (chat_id,)).fetchone()
    return normalize_lang(row[0]) if row and row[0] else normalize_lang(fallback)
def set_language(chat_id, lang):
    lang=normalize_lang(lang)
    with db_connect() as c: c.execute('INSERT INTO pilotes(chat_id,language) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET language=excluded.language',(chat_id,lang))
    return lang

def parse_start_payload(text):
    m=re.match(r"^/start(?:@\w+)?(?:\s+(.+))?\s*$",text or "",re.I); payload=(m.group(1) or "").strip() if m else ""
    if payload.lower().startswith("invite_"): payload=payload[7:]
    return payload.upper() if re.fullmatch(r"[A-Z0-9]{6,32}",payload.upper()) else None
def invitation_link(code): return f"https://t.me/{BOT_USERNAME}?start=invite_{code}" if BOT_USERNAME else None

@app.route('/')
def home(): return "🤖 Telegram bot & OAuth active!"
@app.route('/login')
def login():
    chat_id=request.args.get('chat_id')
    if not chat_id or not CLIENT_SECRET_JSON: return "Missing authentication parameters.",400
    flow=Flow.from_client_config(json.loads(CLIENT_SECRET_JSON),scopes=SCOPES,redirect_uri=f"{RENDER_EXTERNAL_URL}/oauth2callback"); auth_url,state=flow.authorization_url(access_type='offline',prompt='consent',state=chat_id); pending_verifiers[state]=flow.code_verifier; return redirect(auth_url)
@app.route('/oauth2callback')
def oauth2callback():
    chat_id,code=request.args.get('state'),request.args.get('code')
    if not chat_id or not code or not CLIENT_SECRET_JSON: return t('oauth_fail',get_language(chat_id)),400
    try:
        flow=Flow.from_client_config(json.loads(CLIENT_SECRET_JSON),scopes=SCOPES,redirect_uri=f"{RENDER_EXTERNAL_URL}/oauth2callback"); verifier=pending_verifiers.pop(request.args.get('state'),None)
        if not verifier: raise RuntimeError('PKCE verifier not found; restart /register')
        flow.code_verifier=verifier; flow.fetch_token(code=code)
        with db_connect() as c:
            row=c.execute('SELECT code_invitation,language FROM pilotes WHERE chat_id=?',(chat_id,)).fetchone(); code_inv=row[0] if row and row[0] else generer_code_invitation(); lang=normalize_lang(row[1] if row else 'en')
            c.execute('INSERT INTO pilotes(chat_id,code_invitation,google_token,language) VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET code_invitation=excluded.code_invitation,google_token=excluded.google_token',(chat_id,code_inv,flow.credentials.to_json(),lang))
        envoyer_message_telegram(chat_id,t('oauth_success',lang,code=code_inv,invite=invitation_link(code_inv) or f'/join {code_inv}'),parse_mode=None); return f"<h1>{t('oauth_page',lang)}</h1>"
    except Exception as exc: logger.exception('OAuth callback error'); return f'OAuth error: {exc}',500

def telegram_request(method,payload):
    if not TELEGRAM_TOKEN: return False
    try:
        r=requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}',json=payload,timeout=10); return r.ok
    except Exception: logger.exception('Telegram API error'); return False
def envoyer_message_telegram(chat_id,texte,parse_mode='Markdown',reply_markup=None):
    payload={'chat_id':chat_id,'text':texte,'disable_web_page_preview':False}
    if parse_mode: payload['parse_mode']=parse_mode
    if reply_markup: payload['reply_markup']=reply_markup
    if telegram_request('sendMessage',payload): return True
    if parse_mode: payload.pop('parse_mode',None); return telegram_request('sendMessage',payload)
    return False
def answer_callback(callback_id): telegram_request('answerCallbackQuery',{'callback_query_id':callback_id})

def main_keyboard(lang):
    return {'inline_keyboard':[[{'text':t('menu_flights',lang),'callback_data':'vol'},{'text':t('menu_status',lang),'callback_data':'status'}],[{'text':t('menu_invite',lang),'callback_data':'invite'},{'text':t('menu_lang',lang),'callback_data':'lang'}],[{'text':t('menu_reconnect',lang),'callback_data':'reconnect'},{'text':t('menu_disconnect',lang),'callback_data':'disconnect'}]]}
def disconnect_keyboard(lang): return {'inline_keyboard':[[{'text':t('menu_disconnect',lang),'callback_data':'disconnect_yes'},{'text':'❌' if lang=='fr' else '❌', 'callback_data':'disconnect_no'}]]}

def obtenir_token(chat_id):
    with db_connect() as c: row=c.execute('SELECT google_token FROM pilotes WHERE chat_id=?',(chat_id,)).fetchone()
    if not row or not row[0]: return None,None
    try:
        creds=Credentials.from_authorized_user_info(json.loads(row[0]),SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            with db_connect() as c: c.execute('UPDATE pilotes SET google_token=? WHERE chat_id=?',(creds.to_json(),chat_id))
        return creds,None
    except Exception as exc:
        if 'invalid_grant' in str(exc).lower() or 'refresh' in str(exc).lower(): return None,'expired'
        return None,str(exc)

class TTLCache:
    def __init__(self,ttl): self.ttl=ttl; self.data={}; self.lock=threading.RLock()
    def get(self,key,allow_stale=False):
        with self.lock:
            item=self.data.get(key)
            if not item: return None
            value,stamp=item
            if allow_stale or time.time()-stamp<self.ttl: return value
            return None
    def put(self,key,value):
        with self.lock: self.data[key]=(value,time.time())
    def invalidate(self,key=None):
        with self.lock: self.data.clear() if key is None else self.data.pop(key,None)
calendar_cache=TTLCache(CACHE_CALENDAR_TTL); opensky_cache=TTLCache(CACHE_OPENSKY_TTL)

def texte_evenement(event): return '\n'.join(str(event.get(c,'') or '') for c in ('summary','description','location'))
CODES_IATA_EXCLUS={'OFF','DAY','SIM','STB','REC','LAT','VOL','REST','RDO','DUT','DUTY','TRAIN','TRNG','FORM','MCC','LIF','LEAVE','CONG','SICK','VAC','BRF','BRIE','MEET','REP','POS','HOTEL','HOME','BASE','CREW','FLT','FDP','EFB','TBD'}
# Codes ICAO (3 letters) accepted as a fallback for airlines whose calendar
# entries use the ICAO designator instead of the 2-character IATA designator.
# Codes ICAO de compagnies acceptés lorsqu'un agenda utilise le
# désignateur à 3 lettres plutôt que le code IATA à 2 caractères. Cette
# liste est volontairement large et facilement extensible.
CODES_ICAO_COMPAGNIES_BASE={
    # Cargo / express
    'ABW','ABX','ATN','BOX','CAO','CKS','CLX','DHK','DHL','FDX','GEC','GTI',
    'KZR','MPH','NCA','PAC','SQC','TAY','UPS',
    # Passenger and cargo airlines (common ICAO designators)
    'AAL','AAR','ACA','ACI','ADR','AEA','AEE','AFL','AFR','AHY','AIC','ALK',
    'AMX','ANA','ANZ','APW','ASA','ASH','ASL','AUA','AVA','AXM','AZA','BAW',
    'BCS','BEL','BER','BGA','BKP','BMA','BTA','BTI','CAL','CCA','CCM','CES',
    'CFG','CHH','CLH','CMP','CPA','CRK','CSN','CTN','CXA','DAL','DAT','DLH',
    'DTA','EIN','ELY','EMB','ENY','ESY','ETD','ETH','EVA','EZS','EZY','FIN',
    'FJI','FJO','GLO','GLO','GWI','HDA','HKE','IBE','ICE','IGO','JAL','JBU',
    'JST','KAL','KLM','LBT','LOT','LUV','MAU','MEA','MON','MSR','NAX','NOK',
    'NOK','OAL','OMA','PAL','PGT','PIA','QFA','QTR','RAM','RJA','ROT','RYR',
    'SAA','SAS','SBI','SCX','SEH','SIA','SVA','SWR','TAP','TAR','TAY','THA',
    'THY','TRA','TVF','UAE','UAL','VIR','VLG','VOI','WJA','WZZ','XQY',
}
# Optional operator-specific additions, for example EXTRA_AIRLINE_CODES=ABC,XYZ.
EXTRA_AIRLINE_CODES={
    code.strip().upper()
    for code in os.environ.get('EXTRA_AIRLINE_CODES', '').split(',')
    if re.fullmatch(r'[A-Z]{3}', code.strip(), re.IGNORECASE)
}
CODES_ICAO_COMPAGNIES=CODES_ICAO_COMPAGNIES_BASE | EXTRA_AIRLINE_CODES
# A flight prefix is either two alphanumeric characters containing at least
# one letter (3V, U2, 5X, 6E, AF, ...) or a whitelisted 3-letter ICAO code.
# Boundaries prevent accepting TXB235/ABC123 through a 2-character substring.
_PREFIX_2_ALNUM=r'(?=[A-Z0-9]{0,1}[A-Z])[A-Z0-9]{2}'
_PREFIX_3_ICAO=r'(?:' + '|'.join(sorted(CODES_ICAO_COMPAGNIES, reverse=True)) + r')'
REGEX_NUMERO_VOL=re.compile(
    r'(?<![A-Z0-9])(' + _PREFIX_3_ICAO + r'|' + _PREFIX_2_ALNUM +
    r')[- ]?(\d{1,4})(?![A-Z0-9])',
    re.IGNORECASE,
)
REGEX_CODE_IATA=re.compile(r'(?<![A-Z])([A-Z]{3})(?![A-Z])')

def extraire_numero_vol(text):
    for m in REGEX_NUMERO_VOL.finditer(text or ''):
        code=m.group(1).upper()
        if code in CODES_IATA_EXCLUS: continue
        if len(code)==2 or code in CODES_ICAO_COMPAGNIES:
            return f'{code}{m.group(2)}'
    return None
def extraire_codes_iata(text): return sorted({m.group(1) for m in REGEX_CODE_IATA.finditer(text or '') if m.group(1) not in CODES_IATA_EXCLUS})
def evenement_est_un_duty_valide(event): return bool(extraire_numero_vol(texte_evenement(event)) and len(extraire_codes_iata(texte_evenement(event)))>=2)
def _dt(value):
    if not value:return None
    return datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
def duty_info(event):
    text=texte_evenement(event); start=_dt(event.get('start',{}).get('dateTime',event.get('start',{}).get('date'))); end=_dt(event.get('end',{}).get('dateTime',event.get('end',{}).get('date')))
    iata=extraire_codes_iata(text)[:2]; return {'id':event.get('id',''), 'numero':extraire_numero_vol(text), 'iata':iata, 'titre':event.get('summary',''), 'debut_dt':start, 'fin_dt':end, 'event':event}
def _alerter_google_expire(chat_id):
    if not notifier_deja_envoyee(chat_id, '__google__', 'expired'):
        envoyer_message_telegram(chat_id, t('calendar_expired', get_language(chat_id)), reply_markup={'inline_keyboard': [[{'text': t('menu_reconnect', get_language(chat_id)), 'callback_data': 'reconnect'}]]})
        marquer_notification(chat_id, '__google__', 'expired')

def fetch_events(chat_id):
    cached=calendar_cache.get(chat_id)
    if cached is not None:return cached
    creds,error=obtenir_token(chat_id)
    if error=='expired': _alerter_google_expire(chat_id)
    if not creds: return calendar_cache.get(chat_id,allow_stale=True) or []
    try:
        service=build('calendar','v3',credentials=creds); now=datetime.datetime.now(datetime.timezone.utc); events=service.events().list(calendarId='primary',timeMin=now.isoformat(),maxResults=50,singleEvents=True,orderBy='startTime').execute().get('items',[]); calendar_cache.put(chat_id,events); return events
    except Exception as exc:
        logger.warning('Google Calendar error: %s',exc); return calendar_cache.get(chat_id,allow_stale=True) or []
def obtenir_prochain_vol_pour_pilote(chat_id):
    for event in fetch_events(chat_id):
        if evenement_est_un_duty_valide(event): return duty_info(event)
    return None

def verifier_ads_b(numero_vol):
    key=(numero_vol or '').upper(); cached=opensky_cache.get(key)
    if cached is not None:return cached
    try:
        r=requests.get('https://opensky-network.org/api/states/all',timeout=10); states=r.json().get('states',[]) if r.status_code==200 else []
        for s in states:
            if len(s)>8 and s[1] and key in s[1].strip().upper():
                result={'etat':'Au sol 🟢' if s[8] else 'En vol ✈️','altitude':f"{int(s[7]*3.28084) if s[7] is not None else 0} ft",'icao24':s[0],'on_ground':bool(s[8]),'last_contact':s[4]}; opensky_cache.put(key,result); return result
    except Exception: logger.warning('OpenSky error',exc_info=True)
    return opensky_cache.get(key,allow_stale=True)

def calculer_eta(heure_decollage,heure_atterrissage_prevue,maintenant=None):
    maintenant=maintenant or datetime.datetime.now(datetime.timezone.utc); duree=heure_atterrissage_prevue-heure_decollage
    return maintenant+max(datetime.timedelta(0),duree-(maintenant-heure_decollage))
def notifier_deja_envoyee(chat_id,event_id,typ):
    with db_connect() as c: return c.execute('SELECT 1 FROM notification_state WHERE chat_id=? AND event_id=? AND notification_type=?',(chat_id,event_id,typ)).fetchone() is not None
def marquer_notification(chat_id,event_id,typ):
    with db_connect() as c: c.execute('INSERT OR IGNORE INTO notification_state VALUES(?,?,?,?)',(chat_id,event_id,typ,datetime.datetime.now(datetime.timezone.utc).isoformat()))
def rattacher_proche(chat_id,code):
    with db_connect() as c: row=c.execute('SELECT chat_id FROM pilotes WHERE code_invitation=?',(code.upper(),)).fetchone();
    if not row:return False
    with db_connect() as c: c.execute('INSERT INTO proches(chat_id,pilote_chat_id) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET pilote_chat_id=excluded.pilote_chat_id',(chat_id,row[0]))
    return True
def cible_vol(chat_id):
    with db_connect() as c: row=c.execute('SELECT pilote_chat_id FROM proches WHERE chat_id=?',(chat_id,)).fetchone() or c.execute('SELECT chat_id FROM pilotes WHERE chat_id=?',(chat_id,)).fetchone()
    return row[0] if row else None

def recipients(pilot):
    with db_connect() as c: return [pilot]+[r[0] for r in c.execute('SELECT chat_id FROM proches WHERE pilote_chat_id=?',(pilot,))]
def notify(text_key,duty, pilot, **kwargs):
    for cid in recipients(pilot): envoyer_message_telegram(cid,t(text_key,get_language(cid),flight=duty['numero'],iata='/'.join(duty['iata']),**kwargs))

def notification_cycle():
    while True:
        try:
            with db_connect() as c: pilots=[r[0] for r in c.execute("SELECT chat_id FROM pilotes WHERE google_token IS NOT NULL AND google_token != ''")]
            now=datetime.datetime.now(datetime.timezone.utc)
            for pilot in pilots:
                for event in fetch_events(pilot):
                    if not evenement_est_un_duty_valide(event):continue
                    duty=duty_info(event); start,end=duty['debut_dt'],duty['fin_dt']
                    if not start or not end:continue
                    if not notifier_deja_envoyee(pilot,duty['id'],'departure') and datetime.timedelta(0)<=start-now<=datetime.timedelta(minutes=NOTIFICATION_REMINDER_MINUTES):
                        notify('notif_departure',duty,pilot,time=start.strftime('%d/%m %H:%M')); marquer_notification(pilot,duty['id'],'departure')
                    live=verifier_ads_b(duty['numero'])
                    if live and not live['on_ground'] and not notifier_deja_envoyee(pilot,duty['id'],'takeoff'):
                        eta=calculer_eta(start,end,now).strftime('%d/%m %H:%M'); notify('notif_takeoff',duty,pilot,eta=eta); marquer_notification(pilot,duty['id'],'takeoff')
                    if live and live['on_ground'] and notifier_deja_envoyee(pilot,duty['id'],'takeoff') and not notifier_deja_envoyee(pilot,duty['id'],'landing'):
                        notify('notif_landing',duty,pilot); marquer_notification(pilot,duty['id'],'landing')
        except Exception: logger.exception('Notification cycle error')
        time.sleep(NOTIFICATION_INTERVAL_SECONDS)

def traiter_commande_register(chat_id):
    lang=get_language(chat_id)
    try: envoyer_message_telegram(chat_id,t('register_link',lang,link=f'{RENDER_EXTERNAL_URL}/login?chat_id={chat_id}'),parse_mode=None); return True
    except Exception as exc: envoyer_message_telegram(chat_id,t('register_error',lang,error=exc)); return False

def status_text(chat_id):
    lang=get_language(chat_id); creds,error=obtenir_token(chat_id); google=t('connected' if creds else ('expired' if error=='expired' else 'not_connected'),lang)
    with db_connect() as c: contacts=c.execute('SELECT COUNT(*) FROM proches WHERE pilote_chat_id=?',(chat_id,)).fetchone()[0]
    duty=obtenir_prochain_vol_pour_pilote(chat_id); duty_text=(duty['numero']+' ('+'/'.join(duty['iata'])+')') if duty else t('none',lang)
    return t('status',lang,lang='FR' if lang=='fr' else 'EN',google=google,contacts=contacts,duty=duty_text,notifications=t('enabled',lang) if TELEGRAM_TOKEN else t('disabled',lang))

def disconnect_google(chat_id):
    with db_connect() as c: row=c.execute('SELECT google_token FROM pilotes WHERE chat_id=?',(chat_id,)).fetchone()
    if row and row[0]:
        try: requests.post('https://oauth2.googleapis.com/revoke',params={'token':json.loads(row[0]).get('refresh_token') or json.loads(row[0]).get('access_token')},timeout=10)
        except Exception: logger.warning('Google revoke failed',exc_info=True)
    with db_connect() as c: c.execute('UPDATE pilotes SET google_token=NULL WHERE chat_id=?',(chat_id,))
    calendar_cache.invalidate(chat_id)

def traiter_callback(query):
    cid=query.get('message',{}).get('chat',{}).get('id'); data=query.get('data',''); answer_callback(query.get('id'))
    if cid is None:return
    lang=get_language(cid)
    if data=='lang': set_language(cid,'en' if lang=='fr' else 'fr'); envoyer_message_telegram(cid,t('lang_changed_en' if lang=='fr' else 'lang_changed','en' if lang=='fr' else 'fr'),reply_markup=main_keyboard('en' if lang=='fr' else 'fr')); return
    if data=='status': envoyer_message_telegram(cid,status_text(cid),reply_markup=main_keyboard(lang)); return
    if data=='reconnect': traiter_commande_register(cid); return
    if data=='disconnect': envoyer_message_telegram(cid,t('disconnect_confirm',lang),reply_markup=disconnect_keyboard(lang)); return
    if data=='disconnect_yes': disconnect_google(cid); envoyer_message_telegram(cid,t('disconnect_done',lang),reply_markup=main_keyboard(lang)); return
    if data=='disconnect_no': envoyer_message_telegram(cid,t('disconnect_cancel',lang),reply_markup=main_keyboard(lang)); return
    if data=='invite':
        with db_connect() as c: row=c.execute('SELECT code_invitation FROM pilotes WHERE chat_id=?',(cid,)).fetchone()
        envoyer_message_telegram(cid,t('invite',lang,link=invitation_link(row[0])) if row and row[0] and invitation_link(row[0]) else t('invite_config',lang)); return
    if data=='vol': traiter_message({'chat':{'id':cid},'from':{},'text':'/vol'}); return
    envoyer_message_telegram(cid,t('callback_unknown',lang))

def traiter_message(message):
    chat_id=message['chat']['id']; texte=message.get('text','').strip(); lang=get_language(chat_id,normalize_lang(message.get('from',{}).get('language_code','en'))); set_language(chat_id,lang); command=texte.split()[0].split('@',1)[0].lower() if texte.startswith('/') else ''
    if command=='/start':
        code=parse_start_payload(texte); envoyer_message_telegram(chat_id,t('joined' if code and rattacher_proche(chat_id,code) else ('invalid_code' if code else 'welcome'),lang),reply_markup=main_keyboard(lang)); return
    if command in ('/help',): envoyer_message_telegram(chat_id,t('help',lang),reply_markup=main_keyboard(lang)); return
    if command=='/lang':
        arg=texte.split()[1].lower() if len(texte.split())>1 else ''; envoyer_message_telegram(chat_id,t('lang_usage',lang) if arg not in ('fr','en') else t('lang_changed' if arg=='fr' else 'lang_changed_en',arg));
        if arg in ('fr','en'):set_language(chat_id,arg)
        return
    if command in ('/register','/reconnect'): traiter_commande_register(chat_id); return
    if command in ('/moncode','/invite'):
        with db_connect() as c: row=c.execute('SELECT code_invitation FROM pilotes WHERE chat_id=?',(chat_id,)).fetchone()
        if command=='/moncode': envoyer_message_telegram(chat_id,t('code',lang,code=row[0]) if row and row[0] else t('not_registered',lang))
        else: envoyer_message_telegram(chat_id,t('invite',lang,link=invitation_link(row[0])) if row and row[0] and invitation_link(row[0]) else t('invite_config',lang)); return
        return
    if command=='/join': envoyer_message_telegram(chat_id,t('joined',lang) if len(texte.split())>1 and rattacher_proche(chat_id,texte.split()[1]) else t('invalid_code' if len(texte.split())>1 else 'join_usage',lang)); return
    if command=='/stop':
        with db_connect() as c: deleted=c.execute('DELETE FROM proches WHERE chat_id=?',(chat_id,)).rowcount
        envoyer_message_telegram(chat_id,t('stopped' if deleted else 'not_subscribed',lang)); return
    if command=='/disconnect': envoyer_message_telegram(chat_id,t('disconnect_confirm',lang),reply_markup=disconnect_keyboard(lang)); return
    if command=='/status': envoyer_message_telegram(chat_id,status_text(chat_id),reply_markup=main_keyboard(lang)); return
    if command=='/vol':
        target=cible_vol(chat_id)
        if not target: envoyer_message_telegram(chat_id,t('access',lang)); return
        envoyer_message_telegram(chat_id,t('searching',lang)); vol=obtenir_prochain_vol_pour_pilote(target)
        if not vol: envoyer_message_telegram(chat_id,t('no_flight',lang)); return
        live=verifier_ads_b(vol['numero']); status=t('radar_ground' if live and live['etat'].startswith('Au sol') else 'radar_air' if live else 'radar_missing',lang,altitude=live['altitude'] if live else '')
        envoyer_message_telegram(chat_id,t('flight',lang,title=vol['titre'],date=vol['debut_dt'].strftime('%d/%m/%Y à %H:%M'),status=status,link=f"https://www.flightradar24.com/data/flights/{vol['numero']}")); return

def lancer_bot_interactif():
    offset=0
    while True:
        try:
            if not TELEGRAM_TOKEN: time.sleep(10); continue
            data=requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates',params={'offset':offset,'timeout':30},timeout=35).json()
            if data.get('ok'):
                for update in data.get('result',[]):
                    offset=update['update_id']+1
                    if 'callback_query' in update: traiter_callback(update['callback_query'])
                    elif 'message' in update and 'text' in update['message']: traiter_message(update['message'])
        except Exception: logger.exception('Telegram bot loop error'); time.sleep(5)
        time.sleep(2)

if __name__=='__main__':
    threading.Thread(target=lancer_bot_interactif,daemon=True).start(); threading.Thread(target=notification_cycle,daemon=True).start(); app.run(host='0.0.0.0',port=int(os.environ.get('PORT',10000)))
