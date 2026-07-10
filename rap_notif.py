"""
Rap Notif v2 - Surveille pour chaque artiste :
  - Sorties Spotify (albums, singles ET feats)
  - Clips / videos YouTube (via flux RSS, sans cle API)
  - Actu Google News
et envoie une notification Telegram des qu'il y a du nouveau.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

# ============================================================
# CONFIGURATION
# ============================================================

# Artistes a suivre : "Nom affiche": "ID Spotify"
# Pour trouver l'ID : profil Spotify -> Partager -> Copier le lien
# https://open.spotify.com/artist/6Te49r3A6f5BiIgBRxH7FH?si=xxx -> 6Te49r3A6f5BiIgBRxH7FH
ARTISTS = {
    "Ninho": "6Te49r3A6f5BiIgBRxH7FH",
    "SDM": "0LKAV3zJ8a8AIGnyc5OvfB",
    "Bouss": "3hWQDRr1PqwvnHeiZlucBq",
}

# Chaines YouTube a suivre (clips) : "Nom affiche": "ID de la chaine"
# Pour trouver l'ID d'une chaine : va sur la chaine -> ...plus (dans la description)
# -> Partager la chaine -> Copier l'ID de la chaine (commence par UC...)
# Laisser vide {} si tu veux pas suivre YouTube.
YOUTUBE_CHANNELS = {
    # "Ninho": "UCzH3iPCUyoVpnHtcnBCRDMw",
}

# Ignorer les compilations/playlists editoriales dans les feats (recommande)
IGNORE_COMPILATIONS = True

# Anti-doublons : ignore une sortie si un titre quasi identique
# a deja ete notifie (versions sped up, edit, remix du meme single...)
SMART_DEDUP = True

# Mots-cles pour filtrer l'actu Google News (laisser vide [] = tout recevoir)
NEWS_KEYWORDS = ["album", "sortie", "single", "clip", "feat", "featuring", "concert", "tournée", "annonce"]

# M'envoyer une notif Telegram si le bot rencontre une erreur
NOTIFY_ON_ERROR = True

# Secrets (passes en variables d'environnement, JAMAIS en dur dans le code)
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SPOTIFY_CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]

STATE_FILE = Path(__file__).parent / "state.json"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=15)
    if not resp.ok:
        print(f"[ERREUR TELEGRAM] {resp.status_code}: {resp.text}")


def report_error(context: str, error: Exception):
    """Log l'erreur et previent sur Telegram si active."""
    print(f"[ERREUR] {context}: {error}")
    if NOTIFY_ON_ERROR:
        try:
            send_telegram(f"⚠️ <b>Rap Notif — erreur</b>\n\n{context}\n<code>{str(error)[:300]}</code>")
        except Exception:
            pass  # si meme Telegram est en panne, on abandonne silencieusement


# ============================================================
# SPOTIFY
# ============================================================

def get_spotify_token() -> str:
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_artist_releases(token: str, artist_id: str) -> list[dict]:
    """Recupere les 20 dernieres sorties d'un artiste :
    albums, singles ET apparitions en feat sur les projets d'autres artistes."""
    url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={
            "include_groups": "album,single,appears_on",
            "limit": 20,
            "market": "FR",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def format_release_label(rel: dict) -> str:
    if rel.get("album_group") == "appears_on":
        return "🤝 Feat / Apparition"
    if rel["album_type"] == "album":
        return "💿 Album"
    return "🎵 Single"


def normalize_title(title: str) -> str:
    """Normalise un titre pour detecter les doublons :
    'Jolie (Sped Up)' et 'Jolie - Edit' -> 'jolie'"""
    t = title.lower()
    # retire tout ce qui est entre parentheses/crochets et apres un tiret
    t = re.sub(r"[\(\[].*?[\)\]]", "", t)
    t = t.split(" - ")[0]
    # retire les mots-versions courants
    for w in ["sped up", "slowed", "remix", "edit", "version", "instrumental", "acoustic", "live"]:
        t = t.replace(w, "")
    t = re.sub(r"[^a-z0-9à-ÿ]+", "", t)
    return t.strip()


# ============================================================
# YOUTUBE (flux RSS, pas besoin de cle API)
# ============================================================

ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def get_youtube_videos(channel_id: str) -> list[dict]:
    """Recupere les 15 dernieres videos d'une chaine via son flux RSS."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    videos = []
    for entry in root.findall("a:entry", ATOM_NS):
        vid = entry.findtext("yt:videoId", namespaces=ATOM_NS) or ""
        title = entry.findtext("a:title", namespaces=ATOM_NS) or ""
        videos.append({
            "id": vid,
            "title": title,
            "link": f"https://www.youtube.com/watch?v={vid}",
        })
    return videos[:15]


# ============================================================
# GOOGLE NEWS RSS
# ============================================================

def get_news(artist_name: str) -> list[dict]:
    query = requests.utils.quote(f'"{artist_name}" rappeur')
    url = f"https://news.google.com/rss/search?q={query}&hl=fr&gl=FR&ceid=FR:fr"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    articles = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        guid = item.findtext("guid") or link
        articles.append({"title": title, "link": link, "id": guid})
    return articles[:15]


def news_matches_keywords(title: str) -> bool:
    if not NEWS_KEYWORDS:
        return True
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in NEWS_KEYWORDS)


# ============================================================
# ETAT (pour ne pas notifier 2x la meme chose)
# ============================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        state.setdefault("releases", [])
        state.setdefault("titles", [])
        state.setdefault("videos", [])
        state.setdefault("news", [])
        return state
    return {"releases": [], "titles": [], "videos": [], "news": []}


def save_state(state: dict):
    for key in state:
        state[key] = state[key][-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ============================================================
# MAIN
# ============================================================

def main():
    state = load_state()
    first_run = not STATE_FILE.exists()

    # --- Spotify ---
    try:
        token = get_spotify_token()
    except Exception as e:
        report_error("Connexion Spotify impossible (verifie CLIENT_ID/SECRET)", e)
        token = None

    if token:
        for name, artist_id in ARTISTS.items():
            try:
                releases = get_artist_releases(token, artist_id)
            except Exception as e:
                report_error(f"Spotify — {name}", e)
                continue

            for rel in releases:
                if rel["id"] in state["releases"]:
                    continue
                state["releases"].append(rel["id"])

                norm = f"{name}:{normalize_title(rel['name'])}"
                is_dup = SMART_DEDUP and norm in state["titles"]
                state["titles"].append(norm)

                if first_run:
                    continue
                if IGNORE_COMPILATIONS and rel["album_type"] == "compilation":
                    print(f"[IGNORE] Compilation: {rel['name']}")
                    continue
                if is_dup:
                    print(f"[IGNORE] Doublon: {rel['name']}")
                    continue

                label = format_release_label(rel)
                main_artists = ", ".join(a["name"] for a in rel.get("artists", []))
                msg = (
                    f"{label} — <b>{name}</b>\n\n"
                    f"<b>{rel['name']}</b>\n"
                    f"👤 {main_artists}\n"
                    f"📅 {rel['release_date']}\n"
                    f"🔗 {rel['external_urls']['spotify']}"
                )
                send_telegram(msg)
                print(f"[NOTIF] Sortie: {name} - {rel['name']} ({rel.get('album_group')})")

    # --- YouTube ---
    for name, channel_id in YOUTUBE_CHANNELS.items():
        try:
            videos = get_youtube_videos(channel_id)
        except Exception as e:
            report_error(f"YouTube — {name}", e)
            continue

        for vid in videos:
            if vid["id"] in state["videos"]:
                continue
            state["videos"].append(vid["id"])
            if first_run:
                continue

            msg = (
                f"▶️ <b>NOUVELLE VIDÉO — {name}</b>\n\n"
                f"<b>{vid['title']}</b>\n"
                f"🔗 {vid['link']}"
            )
            send_telegram(msg)
            print(f"[NOTIF] Video: {name} - {vid['title']}")

    # --- Google News ---
    for name in ARTISTS:
        try:
            articles = get_news(name)
        except Exception as e:
            report_error(f"Google News — {name}", e)
            continue

        for art in articles:
            if art["id"] in state["news"]:
                continue
            state["news"].append(art["id"])
            if first_run:
                continue
            if not news_matches_keywords(art["title"]):
                continue

            msg = (
                f"📰 <b>ACTU — {name}</b>\n\n"
                f"{art['title']}\n"
                f"🔗 {art['link']}"
            )
            send_telegram(msg)
            print(f"[NOTIF] Actu: {art['title']}")

    save_state(state)
    if first_run:
        send_telegram(
            "✅ Rap Notif v2 est actif !\n"
            "Tu recevras une notif dès qu'il y a du nouveau : sorties Spotify, feats, clips YouTube et actu."
        )
        print("[INIT] Premier lancement : etat initialise, notifications actives au prochain run.")


if __name__ == "__main__":
    main()
