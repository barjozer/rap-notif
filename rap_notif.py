"""
Rap Notif v3 - Surveille pour chaque artiste :
  - Sorties Deezer (albums, singles, EPs) — API publique, aucune cle requise
  - Clips / videos YouTube (via flux RSS, sans cle API)
  - Actu Google News
et envoie une notification Telegram des qu'il y a du nouveau.

(v3 : passage de Spotify a Deezer, car Spotify a restreint son API
pour les nouvelles apps developpeur.)
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

# Artistes a suivre : juste leurs noms, le script trouve tout seul
# leur profil Deezer (et te confirme lequel il a trouve).
ARTISTS = [
    "Ninho",
    "SDM",
    "Bouss",
    "Tiakola",
    "Werenoi",
    "Landy",
    "L2B",
    "Djadja & Dinaz",
    "Maes",
    "Leto",
    "Gazo",
]

# Chaines YouTube a suivre (clips) : "Nom affiche": "ID de la chaine"
# Pour trouver l'ID d'une chaine : va sur la chaine -> ...plus (description)
# -> Partager la chaine -> Copier l'ID de la chaine (commence par UC...)
# Laisser vide {} si tu veux pas suivre YouTube.
YOUTUBE_CHANNELS = {
    # "Ninho": "UCzH3iPCUyoVpnHtcnBCRDMw",
}

# Ignorer les compilations dans les sorties (recommande)
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
# DEEZER (API publique, aucune cle requise)
# ============================================================

def deezer_get(url: str) -> dict:
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"Deezer: {data['error']}")
    return data


def resolve_deezer_artist(name: str, state: dict) -> dict | None:
    """Trouve l'artiste Deezer correspondant au nom (avec cache).
    Retourne {"id": ..., "name": ..., "link": ...} ou None."""
    cache = state.setdefault("deezer_artists", {})
    if name in cache:
        return cache[name]

    query = requests.utils.quote(name)
    data = deezer_get(f"https://api.deezer.com/search/artist?q={query}&limit=1")
    results = data.get("data", [])
    if not results:
        return None

    artist = {
        "id": results[0]["id"],
        "name": results[0]["name"],
        "link": results[0].get("link", f"https://www.deezer.com/artist/{results[0]['id']}"),
    }
    cache[name] = artist
    return artist


def get_deezer_releases(artist_id: int) -> list[dict]:
    """Recupere les 25 dernieres sorties d'un artiste sur Deezer."""
    data = deezer_get(f"https://api.deezer.com/artist/{artist_id}/albums?limit=25")
    return data.get("data", [])


def format_release_label(record_type: str) -> str:
    return {
        "album": "💿 Album",
        "single": "🎵 Single",
        "ep": "🎶 EP",
        "compile": "📦 Compilation",
    }.get(record_type, "🎵 Sortie")


def normalize_title(title: str) -> str:
    """Normalise un titre pour detecter les doublons :
    'Jolie (Sped Up)' et 'Jolie - Edit' -> 'jolie'"""
    t = title.lower()
    t = re.sub(r"[\(\[].*?[\)\]]", "", t)
    t = t.split(" - ")[0]
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
    else:
        state = {}
    state.setdefault("releases", [])
    state.setdefault("titles", [])
    state.setdefault("videos", [])
    state.setdefault("news", [])
    state.setdefault("deezer_artists", {})
    return state


def save_state(state: dict):
    for key in ["releases", "titles", "videos", "news"]:
        state[key] = state[key][-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ============================================================
# MAIN
# ============================================================

def main():
    state = load_state()

    # Mode initialisation par source : si la liste est vide, on enregistre
    # tout l'existant sans notifier (evite le spam au premier passage).
    releases_init = len(state["releases"]) == 0
    videos_init = len(state["videos"]) == 0
    news_init = len(state["news"]) == 0
    newly_resolved = []

    # --- Deezer (sorties) ---
    for name in ARTISTS:
        try:
            was_cached = name in state["deezer_artists"]
            artist = resolve_deezer_artist(name, state)
            if artist is None:
                report_error(f"Deezer — {name}", RuntimeError("artiste introuvable sur Deezer"))
                continue
            if not was_cached:
                newly_resolved.append(artist)

            releases = get_deezer_releases(artist["id"])
        except Exception as e:
            report_error(f"Deezer — {name}", e)
            continue

        for rel in releases:
            rel_id = str(rel["id"])
            if rel_id in state["releases"]:
                continue
            state["releases"].append(rel_id)

            norm = f"{name}:{normalize_title(rel['title'])}"
            is_dup = SMART_DEDUP and norm in state["titles"]
            state["titles"].append(norm)

            if releases_init:
                continue
            record_type = rel.get("record_type", "")
            if IGNORE_COMPILATIONS and record_type == "compile":
                print(f"[IGNORE] Compilation: {rel['title']}")
                continue
            if is_dup:
                print(f"[IGNORE] Doublon: {rel['title']}")
                continue

            label = format_release_label(record_type)
            msg = (
                f"{label} — <b>{artist['name']}</b>\n\n"
                f"<b>{rel['title']}</b>\n"
                f"📅 {rel.get('release_date', '?')}\n"
                f"🔗 {rel.get('link', artist['link'])}"
            )
            send_telegram(msg)
            print(f"[NOTIF] Sortie: {artist['name']} - {rel['title']}")

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
            if videos_init:
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
            if news_init:
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

    # Confirmation des artistes trouves sur Deezer (une seule fois par artiste)
    if newly_resolved:
        lines = "\n".join(f"• <a href=\"{a['link']}\">{a['name']}</a>" for a in newly_resolved)
        send_telegram(
            f"🎧 <b>Rap Notif v3 (Deezer)</b>\n\n"
            f"Artistes surveillés :\n{lines}\n\n"
            f"Clique sur chaque nom pour vérifier que c'est le bon artiste. "
            f"Les notifs de sorties démarrent au prochain passage."
        )
        print("[INIT] Artistes Deezer resolus:", [a["name"] for a in newly_resolved])


if __name__ == "__main__":
    main()
