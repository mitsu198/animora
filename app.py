
import os
import uuid
import requests
import base64
import json
import time
from datetime import datetime, timezone

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify
)
from supabase import create_client

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# Роль назначается только аккаунту с этим подтверждённым email.
ADMIN_EMAIL = "vla2612dik03bir@gmail.com"


def is_admin_user():
    return (
        bool(session.get("user_id"))
        and (session.get("user_email") or "").strip().lower() == ADMIN_EMAIL
    )


@app.context_processor
def inject_user_role():
    return {"is_admin": is_admin_user()}


def get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    if not url or not key:
        raise RuntimeError("Настройки Supabase не найдены")

    return create_client(url, key)


def refresh_auth_session():
    """Обновляет Supabase access token перед истечением срока действия."""
    refresh_token = session.get("refresh_token")
    access_token = session.get("access_token")
    if not refresh_token or not access_token:
        return

    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        expires_at = int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        expires_at = 0

    if expires_at > int(time.time()) + 90:
        return

    response = requests.post(
        f"{os.environ['SUPABASE_URL'].rstrip('/')}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        headers={"apikey": os.environ["SUPABASE_KEY"], "Content-Type": "application/json"},
        json={"refresh_token": refresh_token},
        timeout=20
    )
    if response.status_code >= 400:
        app.logger.warning("SUPABASE SESSION REFRESH FAILED: %s %s", response.status_code, response.text[:500])
        session.clear()
        raise RuntimeError("Срок входа истёк. Войди в аккаунт заново.")
    tokens = response.json()
    session["access_token"] = tokens["access_token"]
    session["refresh_token"] = tokens.get("refresh_token", refresh_token)


def supabase_headers():
    refresh_auth_session()
    return {
        "apikey": os.environ["SUPABASE_KEY"],
        "Authorization": f"Bearer {session['access_token']}",
        "Content-Type": "application/json"
    }


def get_favorites():
    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    response = requests.get(
        f"{base_url}/rest/v1/favorites",
        headers=supabase_headers(),
        params={
            "select": "anime_id,title,poster,genre,created_at",
            "user_id": f"eq.{session['user_id']}",
            "order": "created_at.desc"
        },
        timeout=30
    )
    response.raise_for_status()
    return response.json()


@app.route("/api/anime/catalog")
def anime_catalog_api():
    """Прокси к Jikan: запросы выполняются сервером, а не браузером посетителя."""
    try:
        page = max(1, min(int(request.args.get("page", 1)), 1000))
        response = requests.get(
            "https://api.jikan.moe/v4/anime",
            params={"page": page, "limit": 25, "order_by": "start_date", "sort": "desc"},
            headers={"Accept": "application/json", "User-Agent": "ANIMORA/1.0"},
            timeout=20
        )
        if response.status_code == 429:
            return jsonify({"error": "Источник временно ограничил запросы. Попробуй чуть позже."}), 429
        response.raise_for_status()
        return jsonify(response.json())
    except requests.RequestException:
        app.logger.exception("JIKAN CATALOG API ERROR")
        return jsonify({"error": "Не удалось связаться с каталогом аниме."}), 502
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректный номер страницы."}), 400


# Кэш каталога онгоингов AniVibe. Публичный каталог отдаёт карточки HTML-страницей,
# поэтому читаем его постранично через ?status=Онгоинг&page=N.
ONGOING_CACHE = {"payload": None, "updated_at": 0}
ONGOING_CACHE_TTL = 30 * 60


class AniVibeCatalogParser(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    """Извлекает ссылки на аниме из каталога, не завися от CSS-классов сайта."""
    def __init__(self):
        super().__init__()
        self.items = []
        self.current = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        href = attrs.get("href", "")
        if tag == "a" and __import__("re").search(r"/anime/\d+", href):
            if self.current:
                self._finish()
            self.current = {"href": href, "poster": "", "title": "", "genres": "", "score": None, "year": None, "_text": []}
            self.depth = 1
            return
        if self.current is None:
            return
        if tag == "a":
            self.depth += 1
        elif tag == "img":
            src = attrs.get("data-src") or attrs.get("data-original") or attrs.get("src") or ""
            if src and not src.startswith("data:") and not self.current["poster"]:
                self.current["poster"] = src
            alt = attrs.get("alt", "").strip()
            if alt and not self.current["title"]:
                self.current["title"] = alt

    def handle_data(self, data):
        if self.current is not None:
            value = " ".join(data.split())
            if value:
                self.current["_text"].append(value)

    def handle_endtag(self, tag):
        if self.current is not None and tag == "a":
            self.depth -= 1
            if self.depth <= 0:
                self._finish()

    def _finish(self):
        item = self.current
        if not item:
            return
        text = " ".join(item.pop("_text", []))
        if not item["title"]:
            item["title"] = text
        item["title"] = " ".join(item["title"].split())
        if item["href"] and item["title"]:
            self.items.append(item)
        self.current = None
        self.depth = 0

    def close(self):
        super().close()
        if self.current is not None:
            self._finish()


@app.route("/api/anime/ongoing")
def anime_ongoing_api():
    """Загружает все доступные страницы публичного каталога AniVibe со статусом Онгоинг."""
    now = time.time()
    cached = ONGOING_CACHE.get("payload")
    if cached and now - ONGOING_CACHE.get("updated_at", 0) < ONGOING_CACHE_TTL:
        response = jsonify(cached)
        response.headers["X-Animora-Cache"] = "HIT"
        return response

    try:
        ongoing, seen_ids = [], set()
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Animora/1.0)", "Accept": "text/html"}
        # Берём первую страницу: главная не должна ждать обхода всего каталога.
        response = requests.get(
            f"{ANIVIBE_BASE_URL}/catalog",
            params={"status": "Онгоинг", "page": 1},
            headers=headers,
            timeout=6,
        )
        response.raise_for_status()
        parser = AniVibeCatalogParser()
        parser.feed(response.text)
        for item in parser.items:
            match = __import__("re").search(r"/anime/(\d+)", item["href"])
            if not match:
                continue
            anime_id = match.group(1)
            if anime_id in seen_ids:
                continue
            seen_ids.add(anime_id)
            poster = item.get("poster", "")
            if poster.startswith("/"):
                poster = ANIVIBE_BASE_URL + poster
            ongoing.append({
                "mal_id": anime_id,
                "title": item["title"],
                "title_english": item["title"],
                "images": {"jpg": {"image_url": poster, "large_image_url": poster}},
                "genres": [{"name": name.strip()} for name in item.get("genres", "").split(",") if name.strip()],
                "themes": [],
                "status": "Currently Airing",
                "score": item.get("score"),
                "year": item.get("year"),
                "anivibe_catalog_url": ANIVIBE_BASE_URL + item["href"],
            })

        if not ongoing:
            # Если AniVibe поменял HTML-разметку, используем структурированный
            # Jikan API как резервный источник текущих онгоингов.
            app.logger.warning("AniVibe ongoing parser returned no cards; using Jikan fallback")
            fallback_response = requests.get(
                "https://api.jikan.moe/v4/seasons/now",
                params={"page": 1, "limit": 25},
                headers={"Accept": "application/json", "User-Agent": "ANIMORA/1.0"},
                timeout=8,
            )
            fallback_response.raise_for_status()
            fallback_data = fallback_response.json()
            fallback_items = fallback_data.get("data", [])
            if not fallback_items:
                raise ValueError("AniVibe and Jikan returned no ongoing anime")
            payload = {"data": fallback_items, "source": "jikan-fallback"}
        else:
            payload = {"data": ongoing, "source": "anivibe"}
        ONGOING_CACHE["payload"] = payload
        ONGOING_CACHE["updated_at"] = time.time()
        result = jsonify(payload)
        result.headers["X-Animora-Cache"] = "MISS"
        return result
    except (requests.RequestException, ValueError):
        app.logger.exception("ONGOING ANIME CATALOG ERROR")
        if cached:
            result = jsonify(cached)
            result.headers["X-Animora-Cache"] = "STALE"
            return result
        # Внешние каталоги могут быть временно недоступны из Render.
        # Не превращаем их сбой в ошибку всей главной страницы.
        return jsonify({"data": [], "source": "temporarily-unavailable", "message": "Внешний источник онгоингов временно недоступен."}), 200

@app.route("/api/diagnostics/network")
def network_diagnostics():
    """Проверяет исходящий HTTPS-доступ только к фиксированным безопасным адресам."""
    targets = {
        "internet": "https://example.com/",
        "jikan": "https://api.jikan.moe/",
        "anivibe": "https://anivibe.pro/",
    }
    results = {}
    for name, target in targets.items():
        try:
            response = requests.get(
                target,
                headers={"User-Agent": "ANIMORA-network-check/1.0"},
                timeout=7,
                allow_redirects=True,
                stream=True,
            )
            results[name] = {
                "ok": True,
                "http_status": response.status_code,
            }
            response.close()
        except requests.exceptions.Timeout:
            results[name] = {"ok": False, "error": "timeout"}
        except requests.exceptions.ConnectionError as exc:
            message = str(exc).lower()
            if "network is unreachable" in message:
                reason = "network_unreachable"
            elif "name or service not known" in message or "temporary failure in name resolution" in message:
                reason = "dns_error"
            else:
                reason = "connection_error"
            results[name] = {"ok": False, "error": reason}
        except requests.RequestException:
            results[name] = {"ok": False, "error": "request_error"}

    return jsonify({
        "diagnostic": "outbound_https",
        "results": results,
        "note": "Проверены только фиксированные публичные адреса; ключи и данные аккаунта не используются.",
    })


# AniVibe Partner API: ключ хранится только в переменных окружения Render.
ANIVIBE_BASE_URL = "https://anivibe.pro"
ANIMORA_ALLOWED_ORIGINS = {
    "animora.dpdns.org": "https://animora.dpdns.org",
    "animora-pr-1.onrender.com": "https://animora-pr-1.onrender.com",
    "animora-f6xm.onrender.com": "https://animora-f6xm.onrender.com",
}


def anivibe_request(method, path, *, params=None, payload=None, authenticated=False):
    headers = {"Accept": "application/json"}
    if authenticated:
        api_key = os.environ.get("ANIVIBE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("AniVibe API-ключ не настроен в Render.")
        headers["X-Api-Key"] = api_key
    response = requests.request(
        method,
        f"{ANIVIBE_BASE_URL}{path}",
        params=params,
        json=payload,
        headers=headers,
        timeout=20
    )
    if response.status_code == 429:
        return None, (jsonify({"error": "AniVibe временно ограничил число запросов. Попробуй позже."}), 429)
    if response.status_code == 404:
        return None, (jsonify({"error": "Аниме или серия не найдены в AniVibe."}), 404)
    if response.status_code in (401, 403):
        app.logger.warning("ANIVIBE AUTH/ORIGIN REJECTED: %s", response.status_code)
        return None, (jsonify({"error": "AniVibe не подтвердил ключ или домен ANIMORA. Проверь настройки партнёра."}), 502)
    if response.status_code >= 400:
        app.logger.warning("ANIVIBE API ERROR: %s", response.status_code)
        return None, (jsonify({"error": "AniVibe не смог обработать запрос."}), 502)
    try:
        return response.json(), None
    except ValueError:
        app.logger.warning("ANIVIBE RETURNED INVALID JSON")
        return None, (jsonify({"error": "AniVibe вернул некорректный ответ."}), 502)


@app.route("/api/anivibe/recent")
def anivibe_recent():
    """Постраничная загрузка каталога AniVibe через cursor."""
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 50))
        cursor = request.args.get("cursor", "").strip()
        if len(cursor) > 500:
            return jsonify({"error": "Некорректный курсор каталога."}), 400
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data, error = anivibe_request("GET", "/api/v1/recent", params=params)
        return error if error else jsonify(data)
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректные параметры каталога."}), 400
    except requests.RequestException:
        app.logger.exception("ANIVIBE RECENT CONNECTION ERROR")
        return jsonify({"error": "Не удалось получить последние тайтлы AniVibe."}), 502


@app.route("/api/anivibe/search")
def anivibe_search():
    query = request.args.get("q", "").strip()
    if len(query) < 2 or len(query) > 120:
        return jsonify({"error": "Введи название минимум из двух символов."}), 400
    try:
        limit = max(1, min(int(request.args.get("limit", 10)), 20))
        data, error = anivibe_request("GET", "/api/v1/search", params={"q": query, "limit": limit})
        return error if error else jsonify(data)
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректный лимит поиска."}), 400
    except requests.RequestException:
        app.logger.exception("ANIVIBE SEARCH CONNECTION ERROR")
        return jsonify({"error": "Не удалось связаться с AniVibe."}), 502


@app.route("/api/anivibe/anime/<int:anime_id>")
def anivibe_anime_details(anime_id):
    if anime_id < 1:
        return jsonify({"error": "Некорректный ID аниме."}), 400
    try:
        data, error = anivibe_request("GET", f"/api/v1/anime/{anime_id}")
        return error if error else jsonify(data)
    except requests.RequestException:
        app.logger.exception("ANIVIBE DETAILS CONNECTION ERROR")
        return jsonify({"error": "Не удалось загрузить информацию об аниме."}), 502


@app.route("/api/anivibe/anime/<int:anime_id>/episodes")
def anivibe_episodes(anime_id):
    if anime_id < 1:
        return jsonify({"error": "Некорректный ID аниме."}), 400
    try:
        start = max(1, min(int(request.args.get("from", 1)), 10000))
        limit = max(1, min(int(request.args.get("limit", 50)), 100))
        data, error = anivibe_request(
            "GET", f"/api/v1/anime/{anime_id}/episodes",
            params={"from": start, "limit": limit}
        )
        return error if error else jsonify(data)
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректный диапазон серий."}), 400
    except requests.RequestException:
        app.logger.exception("ANIVIBE EPISODES CONNECTION ERROR")
        return jsonify({"error": "Не удалось загрузить серии."}), 502


@app.route("/api/anivibe/embed-token", methods=["POST"])
def anivibe_embed_token():
    data = request.get_json(silent=True) or {}
    try:
        anime_id = int(data.get("anime_id", 0))
        episode = int(data.get("episode", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректный номер аниме или серии."}), 400
    if anime_id < 1 or episode < 1:
        return jsonify({"error": "Выбери аниме и серию."}), 400

    public_origin = ANIMORA_ALLOWED_ORIGINS.get(request.host.split(":")[0].lower())
    if not public_origin:
        return jsonify({"error": "Этот домен не разрешён для воспроизведения AniVibe."}), 403
    payload = {"anime_id": anime_id, "episode": episode, "origin": public_origin}
    translation = str(data.get("translation", "")).strip()
    if translation:
        if len(translation) > 120:
            return jsonify({"error": "Некорректное название озвучки."}), 400
        payload["translation"] = translation

    try:
        result, error = anivibe_request(
            "POST", "/api/v1/embed-token", payload=payload, authenticated=True
        )
        if error:
            return error
        return jsonify(result)
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503
    except requests.RequestException:
        app.logger.exception("ANIVIBE EMBED TOKEN CONNECTION ERROR")
        return jsonify({"error": "Не удалось получить ссылку на плеер."}), 502


@app.route("/")
def index():
    return render_template("index.html", avatar_url=session.get("avatar_url"), logged_in="user_id" in session, is_admin=is_admin_user())


@app.route("/leaderboard")
def leaderboard():
    # Таблица лидеров временно скрыта до подключения полноценного учёта просмотра.
    return redirect(url_for("index"))


@app.route("/watch-time/increment", methods=["POST"])
def increment_watch_time():
    if "user_id" not in session or "access_token" not in session:
        return jsonify({"success": False, "message": "Войди, чтобы учитывать время просмотра."}), 401

    data = request.get_json(silent=True) or {}
    try:
        seconds = int(data.get("seconds", 0))
    except (TypeError, ValueError):
        seconds = 0
    seconds = max(0, min(seconds, 30))
    if seconds < 1:
        return jsonify({"success": False, "message": "Некорректное время."}), 400

    try:
        base_url = os.environ["SUPABASE_URL"].rstrip("/")
        response = requests.post(
            f"{base_url}/rest/v1/rpc/increment_watch_time",
            headers=supabase_headers(),
            json={
                "p_seconds": seconds,
                "p_display_name": session.get("display_name") or session.get("user_email", "Пользователь").split("@")[0]
            },
            timeout=20
        )
        response.raise_for_status()
        return jsonify({"success": True})
    except Exception:
        app.logger.exception("WATCH TIME UPDATE ERROR")
        return jsonify({"success": False, "message": "Не удалось сохранить время просмотра."}), 503



# --- ANIMORA AniCoins economy ---
def economy_rpc(function_name, payload=None):
    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    response = requests.post(
        f"{base_url}/rest/v1/rpc/{function_name}",
        headers=supabase_headers(),
        json=payload or {},
        timeout=20
    )
    if response.status_code >= 400:
        app.logger.warning("ECONOMY RPC %s failed: %s %s", function_name, response.status_code, response.text[:400])
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = ""
        if "Not enough AniCoins" in message:
            raise ValueError("Недостаточно AniCoins для покупки.")
        if "requirements not met" in message:
            raise ValueError("Условия достижения ещё не выполнены.")
        if "already claimed" in message or "already owned" in message:
            raise ValueError("Награда уже получена или предмет уже куплен.")
        raise RuntimeError("Не удалось выполнить операцию экономики. Проверь, применена ли миграция Supabase.")
    try:
        return response.json()
    except ValueError:
        return None


def economy_wallet():
    result = economy_rpc("ensure_animora_wallet")
    if isinstance(result, list):
        return result[0] if result else {"balance": 0, "xp": 0, "level": 1, "premium_until": None}
    return result or {"balance": 0, "xp": 0, "level": 1, "premium_until": None}


def economy_get_rows(table, select):
    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    response = requests.get(
        f"{base_url}/rest/v1/{table}",
        headers=supabase_headers(),
        params={"select": select, "user_id": f"eq.{session['user_id']}"},
        timeout=20
    )
    response.raise_for_status()
    return response.json()


@app.route("/achievements")
def achievements():
    if "user_id" not in session:
        flash("Войди в аккаунт, чтобы получать награды.")
        return redirect(url_for("login"))
    definitions = [
        {"key":"first_favorite","icon":"💜","title":"Первая любовь","description":"Добавь первое аниме в избранное.","coins":100,"xp":25},
        {"key":"collector_5","icon":"🎬","title":"Коллекционер","description":"Собери в избранном 5 аниме.","coins":250,"xp":60},
        {"key":"watch_30m","icon":"⏱️","title":"Первый марафон","description":"Набери 30 минут просмотра с авторизацией.","coins":200,"xp":50},
        {"key":"watch_2h","icon":"🌙","title":"Ночная смена","description":"Набери 2 часа просмотра с авторизацией.","coins":600,"xp":150},
    ]
    try:
        wallet = economy_wallet()
        completed = {row.get("achievement_key") for row in economy_get_rows("animora_achievements", "achievement_key")}
        favorites = economy_get_rows("favorites", "anime_id")
        watch_rows = economy_get_rows("animora_watch_time", "total_seconds")
        watch_seconds = int(watch_rows[0].get("total_seconds", 0)) if watch_rows else 0
        favorite_count = len(favorites)
        for item in definitions:
            item["claimed"] = item["key"] in completed
            item["eligible"] = (
                (item["key"] == "first_favorite" and favorite_count >= 1)
                or (item["key"] == "collector_5" and favorite_count >= 5)
                or (item["key"] == "watch_30m" and watch_seconds >= 1800)
                or (item["key"] == "watch_2h" and watch_seconds >= 7200)
            )
        return render_template("achievements.html", wallet=wallet, achievements=definitions)
    except Exception:
        app.logger.exception("ANIMORA ACHIEVEMENTS LOAD ERROR")
        flash("Не удалось загрузить достижения. Проверь миграцию supabase_economy.sql в Supabase.")
        return redirect(url_for("profile"))


@app.route("/shop")
def shop():
    if "user_id" not in session:
        flash("Войди в аккаунт, чтобы открыть магазин.")
        return redirect(url_for("login"))
    item_defs = [
        {"key":"banner_sakura","icon":"🌸","title":"Баннер «Сакура»","description":"Нежный розово-фиолетовый баннер для профиля.","price":1500},
        {"key":"banner_neon","icon":"🌌","title":"Баннер «Неон»","description":"Неоновое оформление в стиле ночного города.","price":2500},
        {"key":"banner_galaxy","icon":"🌠","title":"Баннер «Галактика»","description":"Редкий космический баннер.","price":4000},
        {"key":"frame_violet","icon":"💜","title":"Фиолетовая рамка","description":"Украшение для аватара.","price":1000},
        {"key":"frame_gold","icon":"👑","title":"Золотая рамка","description":"Премиальная золотая рамка.","price":3000},
    ]
    try:
        wallet = economy_wallet()
        owned = {row.get("item_key") for row in economy_get_rows("animora_inventory", "item_key")}
        for item in item_defs:
            item["owned"] = item["key"] in owned
        return render_template("shop.html", wallet=wallet, items=item_defs, premium_plans=[
            {"days":7,"price":5000},{"days":30,"price":15000},{"days":90,"price":40000}
        ])
    except Exception:
        app.logger.exception("ANIMORA SHOP LOAD ERROR")
        flash("Не удалось загрузить магазин. Проверь миграцию supabase_economy.sql в Supabase.")
        return redirect(url_for("profile"))


@app.route("/economy/claim-achievement", methods=["POST"])
def claim_economy_achievement():
    if "user_id" not in session or "access_token" not in session:
        return jsonify({"success":False,"message":"Войди в аккаунт."}), 401
    key = str((request.get_json(silent=True) or {}).get("key", "")).strip()
    if key not in {"first_favorite","collector_5","watch_30m","watch_2h"}:
        return jsonify({"success":False,"message":"Неизвестное достижение."}), 400
    try:
        result = economy_rpc("claim_animora_achievement", {"p_key": key})
        return jsonify({"success":bool((result or {}).get("success", True)),"message":"Награда начислена!"})
    except ValueError as error:
        return jsonify({"success":False,"message":str(error)}), 409
    except Exception:
        app.logger.exception("ANIMORA ACHIEVEMENT CLAIM ERROR")
        return jsonify({"success":False,"message":"Не удалось получить награду. Проверь настройки экономики в Supabase."}), 503


@app.route("/economy/buy-item", methods=["POST"])
def buy_economy_item():
    if "user_id" not in session or "access_token" not in session:
        return jsonify({"success":False,"message":"Войди в аккаунт."}), 401
    key = str((request.get_json(silent=True) or {}).get("key", "")).strip()
    if key not in {"banner_sakura","banner_neon","banner_galaxy","frame_violet","frame_gold"}:
        return jsonify({"success":False,"message":"Неизвестный предмет."}), 400
    try:
        economy_rpc("buy_animora_item", {"p_item_key": key})
        return jsonify({"success":True,"message":"Покупка прошла успешно!"})
    except ValueError as error:
        return jsonify({"success":False,"message":str(error)}), 409
    except Exception:
        app.logger.exception("ANIMORA SHOP PURCHASE ERROR")
        return jsonify({"success":False,"message":"Не удалось купить предмет. Проверь миграцию Supabase."}), 503


@app.route("/economy/buy-premium", methods=["POST"])
def buy_economy_premium():
    if "user_id" not in session or "access_token" not in session:
        return jsonify({"success":False,"message":"Войди в аккаунт."}), 401
    try:
        days = int((request.get_json(silent=True) or {}).get("days", 0))
    except (TypeError, ValueError):
        days = 0
    if days not in {7,30,90}:
        return jsonify({"success":False,"message":"Недопустимый срок Premium."}), 400
    try:
        economy_rpc("buy_animora_premium", {"p_days": days})
        return jsonify({"success":True,"message":"ANIMORA Premium активирован!"})
    except ValueError as error:
        return jsonify({"success":False,"message":str(error)}), 409
    except Exception:
        app.logger.exception("ANIMORA PREMIUM PURCHASE ERROR")
        return jsonify({"success":False,"message":"Не удалось подключить Premium. Проверь миграцию Supabase."}), 503


@app.route("/favorites/list")
def favorites_list():
    if "user_id" not in session or "access_token" not in session:
        return jsonify({
            "success": False,
            "message": "Необходимо войти в аккаунт."
        }), 401

    try:
        favorites = get_favorites()
        anime_ids = [item["anime_id"] for item in favorites]

        return jsonify({
            "success": True,
            "anime_ids": anime_ids
        })

    except Exception:
        app.logger.exception("FAVORITES LIST ERROR")
        return jsonify({
            "success": False,
            "message": "Не удалось загрузить избранное."
        }), 500


@app.route("/anime")
def anime():
    from flask import send_from_directory
    return send_from_directory(".", "anime.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Заполни все поля.")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("Пароль должен содержать минимум 6 символов.")
            return redirect(url_for("register"))

        try:
            result = get_supabase().auth.sign_up({
                "email": email,
                "password": password
            })

            if result.session:
                session["user_email"] = result.user.email
                session["user_id"] = result.user.id
                session["access_token"] = result.session.access_token
                session["refresh_token"] = result.session.refresh_token

                metadata = result.user.user_metadata or {}
                session["display_name"] = metadata.get(
                    "display_name", email.split("@")[0]
                )

                return redirect(url_for("profile"))

            flash("Теперь войди в аккаунт.")
            return redirect(url_for("login"))

        except Exception:
            app.logger.exception("SUPABASE REGISTRATION ERROR")
            flash("Не удалось зарегистрироваться. Попробуй снова.")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        try:
            result = get_supabase().auth.sign_in_with_password({
                "email": email,
                "password": password
            })

            session["user_email"] = result.user.email
            session["user_id"] = result.user.id
            session["access_token"] = result.session.access_token
            session["refresh_token"] = result.session.refresh_token

            metadata = result.user.user_metadata or {}
            session["display_name"] = metadata.get(
                "display_name", email.split("@")[0]
            )

            return redirect(url_for("profile"))

        except Exception:
            app.logger.exception("SUPABASE LOGIN ERROR")
            flash("Не удалось войти. Проверь почту и пароль.")

    return render_template("login.html")


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user_id" not in session:
        flash("Сначала войди в аккаунт.")
        return redirect(url_for("login"))

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_name":
            display_name = request.form.get("display_name", "").strip()

            if len(display_name) < 2 or len(display_name) > 24:
                flash("Имя должно содержать от 2 до 24 символов.")
                return redirect(url_for("profile"))

            try:
                base_url = os.environ["SUPABASE_URL"].rstrip("/")
                response = requests.put(
                    f"{base_url}/auth/v1/user",
                    headers=supabase_headers(),
                    json={"data": {"display_name": display_name}},
                    timeout=30
                )
                response.raise_for_status()
                session["display_name"] = display_name
                flash("Имя пользователя успешно изменено!")

            except Exception:
                app.logger.exception("PROFILE NAME UPDATE ERROR")
                flash("Не удалось изменить имя. Попробуй войти заново.")

            return redirect(url_for("profile"))

        if action == "upload_avatar":
            image = request.files.get("avatar")

            if not image or not image.filename:
                flash("Сначала выбери изображение.")
                return redirect(url_for("profile"))

            allowed_types = {
                "image/jpeg", "image/png",
                "image/webp", "image/gif"
            }

            if image.mimetype not in allowed_types:
                flash("Можно загружать только JPG, PNG, WEBP или GIF.")
                return redirect(url_for("profile"))

            image_data = image.read()

            if len(image_data) > 5 * 1024 * 1024:
                flash("Изображение должно быть не больше 5 МБ.")
                return redirect(url_for("profile"))

            try:
                base_url = os.environ["SUPABASE_URL"].rstrip("/")
                user_id = session["user_id"]

                response = requests.post(
                    f"{base_url}/storage/v1/object/avatars/{user_id}/avatar",
                    headers={
                        **supabase_headers(),
                        "Content-Type": image.mimetype,
                        "x-upsert": "true"
                    },
                    data=image_data,
                    timeout=30
                )
                response.raise_for_status()

                session["avatar_url"] = (
                    f"{base_url}/storage/v1/object/public/avatars/"
                    f"{user_id}/avatar?v={uuid.uuid4().hex}"
                )
                flash("Аватар успешно обновлён!")

            except Exception:
                app.logger.exception("AVATAR UPLOAD ERROR")
                flash("Не удалось загрузить аватар. Попробуй снова.")

            return redirect(url_for("profile"))

    try:
        favorites = get_favorites()
    except Exception:
        app.logger.exception("FAVORITES LOAD ERROR")
        favorites = []
        flash("Не удалось загрузить избранное.")

    try:
        wallet = economy_wallet()
        settings_rows = economy_get_rows("animora_profile_settings", "banner_key,frame_key")
        profile_settings = settings_rows[0] if settings_rows else {}
    except Exception:
        app.logger.exception("PROFILE WALLET LOAD ERROR")
        wallet = {"balance": 0, "xp": 0, "level": 1, "premium_until": None}
        profile_settings = {}
    premium_until = wallet.get("premium_until") if isinstance(wallet, dict) else None
    try:
        premium_active = bool(premium_until) and datetime.fromisoformat(str(premium_until).replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        premium_active = False
    return render_template(
        "profile.html",
        wallet=wallet,
        premium_active=premium_active,
        profile_settings=profile_settings,
        email=session.get("user_email"),
        avatar_url=session.get("avatar_url"),
        display_name=session.get("display_name", "Пользователь"),
        favorites=favorites,
        is_admin=is_admin_user()
    )


@app.route("/favorites/toggle", methods=["POST"])
def toggle_favorite():
    if "user_id" not in session or "access_token" not in session:
        return jsonify({
            "success": False,
            "message": "Сначала войди в аккаунт."
        }), 401

    data = request.get_json(silent=True) or {}
    anime_id = str(data.get("anime_id", "")).strip()
    title = str(data.get("title", "")).strip()
    poster = str(data.get("poster", "")).strip()
    genre = str(data.get("genre", "")).strip()

    if not anime_id or not title:
        return jsonify({
            "success": False,
            "message": "Не хватает данных об аниме."
        }), 400

    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    url = f"{base_url}/rest/v1/favorites"
    headers = supabase_headers()

    try:
        check = requests.get(
            url,
            headers=headers,
            params={
                "select": "id",
                "user_id": f"eq.{session['user_id']}",
                "anime_id": f"eq.{anime_id}"
            },
            timeout=30
        )
        check.raise_for_status()
        existing = check.json()

        if existing:
            response = requests.delete(
                url,
                headers=headers,
                params={
                    "user_id": f"eq.{session['user_id']}",
                    "anime_id": f"eq.{anime_id}"
                },
                timeout=30
            )
            response.raise_for_status()

            return jsonify({
                "success": True,
                "favorited": False,
                "message": "Удалено из избранного."
            })

        response = requests.post(
            url,
            headers={**headers, "Prefer": "return=minimal"},
            json={
                "user_id": session["user_id"],
                "anime_id": anime_id,
                "title": title,
                "poster": poster,
                "genre": genre
            },
            timeout=30
        )
        response.raise_for_status()

        return jsonify({
            "success": True,
            "favorited": True,
            "message": "Добавлено в избранное!"
        })

    except requests.HTTPError as error:
        upstream = error.response
        app.logger.error("FAVORITE TOGGLE SUPABASE ERROR: %s %s", upstream.status_code, upstream.text[:1000])
        if upstream.status_code == 401:
            return jsonify({"success": False, "message": "Срок входа истёк. Выйди из аккаунта и войди снова."}), 401
        return jsonify({"success": False, "message": "Supabase отклонил изменение избранного. Проверь настройки таблицы favorites и RLS."}), 502
    except RuntimeError as error:
        return jsonify({"success": False, "message": str(error)}), 401
    except Exception:
        app.logger.exception("FAVORITE TOGGLE ERROR")
        return jsonify({
            "success": False,
            "message": "Внутренняя ошибка при обновлении избранного. Подробности записаны в лог."
        }), 500


@app.route("/logout")
def logout():
    session.clear()
    flash("Ты вышел из аккаунта.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
