"""
Rap Notif v4 - Surveille pour chaque artiste :
  - Sorties Deezer (albums, singles, EPs) — API publique, aucune cle requise
  - Clips / videos YouTube (via flux RSS, sans cle API)
  - Actu Google News
et envoie une notification Telegram des qu'il y a du nouveau.

Nouveautes v4 :
  - Pochettes d'albums affichees dans les notifs de sorties
  - Commandes Telegram : /add NomArtiste, /remove NomArtiste, /list, /help
    (traitees a chaque reveil du bot, donc effet sous ~15-20 min)
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

# Artistes de base : juste leurs noms, le script trouve tout seul
# leur profil Deezer (et te confirme lequel il a trouve).
# Tu peux aussi en ajouter/retirer depuis Telegram avec /add et /remove.
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

# Artistes pour lesquels tu veux recevoir l'actu Google News.
# Les sorties Deezer restent surveillees pour TOUS les artistes,
# mais l'actu n'est envoyee que pour ceux listes ici.
NEWS_ARTISTS = [
    "Bouss",
]

# Chaines YouTube a suivre (clips) : "Nom affiche": "ID de la chaine"
# (ID = va sur la chaine -> ...plus -> Partager la chaine -> Copier l'ID, commence par UC)
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
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text: str):
    resp = requests.post(f"{TG_API}/sendMessage", json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=15)
    if not resp.ok:
        print(f"[ERREUR TELEGRAM] {resp.status_code}: {resp.text}")


def send_telegram_photo(photo_url: str, caption: str):
    """Envoie une notif avec image (pochette). Retombe sur du texte si echec."""
    resp = requests.post(f"{TG_API}/sendPhoto", json={
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }, timeout=20)
    if not resp.ok:
        print(f"[ERREUR TELEGRAM PHOTO] {resp.status_code}: {resp.text}")
        send_telegram(caption)


def report_error(context: str, error: Exception):
    print(f"[ERREUR] {context}: {error}")
    if NOTIFY_ON_ERROR:
        try:
            send_telegram(f"⚠️ <b>Rap Notif — erreur</b>\n\n{context}\n<code>{str(error)[:300]}</code>")
        except Exception:
            pass


# ============================================================
# COMMANDES TELEGRAM (/add, /remove, /list, /help)
# ============================================================

HELP_TEXT = (
    "🤖 <b>Commandes Rap Notif</b>\n\n"
    "/add NomArtiste — suivre un nouvel artiste\n"
    "/remove NomArtiste — arrêter de suivre un artiste\n"
    "/last NomArtiste — sa dernière sortie (n'importe quel artiste)\n"
    "/top NomArtiste — ses 5 sons les plus écoutés\n"
    "/news NomArtiste — ses 3 derniers articles d'actu\n"
    "/stats — les stats du mois\n"
    "/list — voir les artistes suivis\n"
    "/help — afficher cette aide\n\n"
    "<i>Je me réveille toutes les ~15-20 min, donc tes commandes "
    "sont prises en compte au réveil suivant.</i>"
)


def get_current_artists(state: dict) -> list[str]:
    """Liste effective = artistes du code + ajouts Telegram - retraits Telegram."""
    added = state.get("added_artists", [])
    removed = [r.lower() for r in state.get("removed_artists", [])]
    merged = list(ARTISTS)
    for a in added:
        if a.lower() not in [m.lower() for m in merged]:
            merged.append(a)
    return [a for a in merged if a.lower() not in removed]


def process_telegram_commands(state: dict):
    """Lit les nouveaux messages Telegram et applique les commandes."""
    offset = state.get("tg_offset", 0)
    try:
        resp = requests.get(f"{TG_API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=15)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as e:
        report_error("Lecture des commandes Telegram", e)
        return

    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()

        # Securite : on n'accepte les commandes que depuis TON chat
        if chat_id != str(TELEGRAM_CHAT_ID) or not text.startswith("/"):
            continue

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/add" and arg:
            current = get_current_artists(state)
            if arg.lower() in [a.lower() for a in current]:
                send_telegram(f"ℹ️ <b>{arg}</b> est déjà suivi.")
                continue
            state.setdefault("added_artists", []).append(arg)
            state["removed_artists"] = [
                r for r in state.get("removed_artists", []) if r.lower() != arg.lower()
            ]
            send_telegram(f"✅ <b>{arg}</b> ajouté ! Je cherche son profil Deezer...")
            print(f"[CMD] /add {arg}")

        elif cmd == "/remove" and arg:
            current = get_current_artists(state)
            match = next((a for a in current if a.lower() == arg.lower()), None)
            if match is None:
                send_telegram(f"ℹ️ <b>{arg}</b> n'est pas dans la liste. Envoie /list pour voir les artistes suivis.")
                continue
            state.setdefault("removed_artists", []).append(match)
            state["added_artists"] = [
                a for a in state.get("added_artists", []) if a.lower() != match.lower()
            ]
            send_telegram(f"🗑 <b>{match}</b> retiré, tu ne recevras plus ses sorties.")
            print(f"[CMD] /remove {match}")

        elif cmd == "/last" and arg:
            try:
                artist = resolve_deezer_artist(arg, state)
                if artist is None:
                    send_telegram(f"❌ Je trouve pas <b>{arg}</b> sur Deezer, vérifie l'orthographe.")
                    continue
                releases = get_deezer_releases(artist["id"])
                if not releases:
                    send_telegram(f"ℹ️ Aucune sortie trouvée pour <b>{artist['name']}</b>.")
                    continue
                # La plus recente par date de sortie
                latest = max(releases, key=lambda r: r.get("release_date", ""))
                label = format_release_label(latest.get("record_type", ""))
                caption = (
                    f"{label} — <b>{artist['name']}</b> (dernière sortie)\n\n"
                    f"<b>{latest['title']}</b>\n"
                    f"📅 {latest.get('release_date', '?')}\n"
                    f"🔗 {latest.get('link', artist['link'])}"
                )
                cover = latest.get("cover_xl") or latest.get("cover_big") or latest.get("cover")
                if cover:
                    send_telegram_photo(cover, caption)
                else:
                    send_telegram(caption)
                print(f"[CMD] /last {artist['name']} -> {latest['title']}")
            except Exception as e:
                report_error(f"/last {arg}", e)

        elif cmd == "/top" and arg:
            try:
                artist = resolve_deezer_artist(arg, state)
                if artist is None:
                    send_telegram(f"❌ Je trouve pas <b>{arg}</b> sur Deezer, vérifie l'orthographe.")
                    continue
                tracks = get_deezer_top_tracks(artist["id"])
                if not tracks:
                    send_telegram(f"ℹ️ Aucun son trouvé pour <b>{artist['name']}</b>.")
                    continue
                lines = "\n".join(
                    f"{i}. <a href=\"{t.get('link', '')}\">{t['title']}</a>"
                    for i, t in enumerate(tracks, 1)
                )
                send_telegram(f"🏆 <b>Top 5 — {artist['name']}</b>\n\n{lines}")
                print(f"[CMD] /top {artist['name']}")
            except Exception as e:
                report_error(f"/top {arg}", e)

        elif cmd == "/news" and arg:
            try:
                articles = get_news(arg)
                if not articles:
                    send_telegram(f"ℹ️ Aucun article récent trouvé pour <b>{arg}</b>.")
                    continue
                lines = "\n\n".join(
                    f"• <a href=\"{a['link']}\">{a['title']}</a>"
                    for a in articles[:3]
                )
                send_telegram(f"📰 <b>Dernières actus — {arg}</b>\n\n{lines}")
                print(f"[CMD] /news {arg}")
            except Exception as e:
                report_error(f"/news {arg}", e)

        elif cmd == "/stats":
            from datetime import datetime, timezone
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            stats = state.get("stats", {}).get(month, {})
            total = stats.get("releases", 0)
            per_artist = stats.get("per_artist", {})
            if total == 0:
                send_telegram(f"📊 <b>Stats du mois</b>\n\nAucune sortie notifiée ce mois-ci pour l'instant. Ça va venir 🎤")
            else:
                ranking = sorted(per_artist.items(), key=lambda x: -x[1])
                lines = "\n".join(f"• {name} : {n}" for name, n in ranking)
                send_telegram(
                    f"📊 <b>Stats du mois</b>\n\n"
                    f"🎵 {total} sortie(s) notifiée(s)\n\n{lines}"
                )
            print("[CMD] /stats")

        elif cmd == "/list":
            current = get_current_artists(state)
            listing = "\n".join(f"• {a}" for a in current) or "(aucun)"
            send_telegram(f"🎤 <b>Artistes suivis ({len(current)})</b>\n\n{listing}")

        else:
            send_telegram(HELP_TEXT)


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
    """Trouve l'artiste Deezer correspondant au nom (avec cache)."""
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


def get_deezer_top_tracks(artist_id: int, limit: int = 5) -> list[dict]:
    """Recupere les sons les plus ecoutes d'un artiste sur Deezer."""
    data = deezer_get(f"https://api.deezer.com/artist/{artist_id}/top?limit={limit}")
    return data.get("data", [])


def format_release_label(record_type: str) -> str:
    return {
        "album": "💿 Album",
        "single": "🎵 Single",
        "ep": "🎶 EP",
        "compile": "📦 Compilation",
    }.get(record_type, "🎵 Sortie")


def normalize_title(title: str) -> str:
    """'Jolie (Sped Up)' et 'Jolie - Edit' -> 'jolie'"""
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
    state.setdefault("init_release_artists", [])
    state.setdefault("init_video_channels", [])
    state.setdefault("init_news_artists", [])
    state.setdefault("added_artists", [])
    state.setdefault("removed_artists", [])
    state.setdefault("tg_offset", 0)
    return state


def save_state(state: dict):
    for key in ["releases", "titles", "videos", "news"]:
        state[key] = state[key][-1000:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ============================================================
# MAIN
# ============================================================

def main():
    state = load_state()

    # 1) Traiter les commandes Telegram recues depuis le dernier passage
    process_telegram_commands(state)

    artists = get_current_artists(state)
    newly_resolved = []

    # 2) Deezer (sorties)
    for name in artists:
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

        # Premier passage pour CET artiste : memorisation silencieuse du catalogue
        artist_init = name not in state["init_release_artists"]

        for rel in releases:
            rel_id = str(rel["id"])
            if rel_id in state["releases"]:
                continue
            state["releases"].append(rel_id)

            norm = f"{name}:{normalize_title(rel['title'])}"
            is_dup = SMART_DEDUP and norm in state["titles"]
            state["titles"].append(norm)

            if artist_init:
                continue
            record_type = rel.get("record_type", "")
            if IGNORE_COMPILATIONS and record_type == "compile":
                print(f"[IGNORE] Compilation: {rel['title']}")
                continue
            if is_dup:
                print(f"[IGNORE] Doublon: {rel['title']}")
                continue

            label = format_release_label(record_type)
            caption = (
                f"{label} — <b>{artist['name']}</b>\n\n"
                f"<b>{rel['title']}</b>\n"
                f"📅 {rel.get('release_date', '?')}\n"
                f"🔗 {rel.get('link', artist['link'])}"
            )
            cover = rel.get("cover_xl") or rel.get("cover_big") or rel.get("cover")
            if cover:
                send_telegram_photo(cover, caption)
            else:
                send_telegram(caption)
            print(f"[NOTIF] Sortie: {artist['name']} - {rel['title']}")

            # Stats mensuelles
            from datetime import datetime, timezone
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            month_stats = state.setdefault("stats", {}).setdefault(month, {"releases": 0, "per_artist": {}})
            month_stats["releases"] += 1
            month_stats["per_artist"][artist["name"]] = month_stats["per_artist"].get(artist["name"], 0) + 1

        if artist_init:
            state["init_release_artists"].append(name)
            print(f"[INIT] Catalogue memorise pour {name}")

    # 3) YouTube
    for name, channel_id in YOUTUBE_CHANNELS.items():
        try:
            videos = get_youtube_videos(channel_id)
        except Exception as e:
            report_error(f"YouTube — {name}", e)
            continue

        channel_init = channel_id not in state["init_video_channels"]

        for vid in videos:
            if vid["id"] in state["videos"]:
                continue
            state["videos"].append(vid["id"])
            if channel_init:
                continue

            msg = (
                f"▶️ <b>NOUVELLE VIDÉO — {name}</b>\n\n"
                f"<b>{vid['title']}</b>\n"
                f"🔗 {vid['link']}"
            )
            send_telegram(msg)
            print(f"[NOTIF] Video: {name} - {vid['title']}")

        if channel_init:
            state["init_video_channels"].append(channel_id)
            print(f"[INIT] Videos memorisees pour {name}")

    # 4) Google News (uniquement pour NEWS_ARTISTS, s'ils sont toujours suivis)
    for name in NEWS_ARTISTS:
        if name.lower() not in [a.lower() for a in artists]:
            continue
        try:
            articles = get_news(name)
        except Exception as e:
            report_error(f"Google News — {name}", e)
            continue

        news_init = name not in state["init_news_artists"]

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

        if news_init:
            state["init_news_artists"].append(name)
            print(f"[INIT] Actu memorisee pour {name}")

    save_state(state)

    # 5) Confirmation des artistes nouvellement trouves sur Deezer
    if newly_resolved:
        lines = "\n".join(f"• <a href=\"{a['link']}\">{a['name']}</a>" for a in newly_resolved)
        send_telegram(
            f"🎧 <b>Rap Notif</b> — nouveaux artistes surveillés :\n{lines}\n\n"
            f"Clique sur chaque nom pour vérifier que c'est le bon artiste. "
            f"Les notifs démarrent au prochain passage."
        )
        print("[INIT] Artistes Deezer resolus:", [a["name"] for a in newly_resolved])


if __name__ == "__main__":
    main()
