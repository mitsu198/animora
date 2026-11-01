
import os
import uuid
import requests
import base64
import json
import time

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


def get_watchlist():
    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    response = requests.get(
        f"{base_url}/rest/v1/animora_watchlist",
        headers=supabase_headers(),
        params={"select": "anime_id,title,poster,genre,status,updated_at", "user_id": f"eq.{session['user_id']}", "order": "updated_at.desc"},
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


@app.route("/favorites/list")
def favorites_list():
    if "user_id" not in session or "access_token" not in session:
        return jsonify({
            "success": False,
            "message": "Необходимо войти в аккаунт."
        }), 401

    if is_admin_user():
        try:
            base_url = os.environ["SUPABASE_URL"].rstrip("/")
            user_response = requests.get(
                f"{base_url}/auth/v1/user",
                headers=supabase_headers(),
                timeout=20
            )
            user_response.raise_for_status()
            metadata = user_response.json().get("user_metadata") or {}
            session["animora_level"] = max(1, int(metadata.get("animora_level", 1) or 1))
            session["animora_xp"] = max(0, int(metadata.get("animora_xp", 0) or 0))
        except Exception:
            app.logger.exception("ADMIN PROGRESS LOAD ERROR")

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


def establish_auth_session(result, email):
    """Store a Supabase password-auth session in Flask's signed session."""
    if not result.user or not result.session:
        raise RuntimeError(
            "Аккаунт создан, но Supabase не выдал сессию. "
            "Отключи подтверждение Email в Supabase Auth → Providers → Email."
        )
    session.clear()
    session["user_email"] = result.user.email or email
    session["user_id"] = result.user.id
    session["access_token"] = result.session.access_token
    session["refresh_token"] = result.session.refresh_token
    metadata = result.user.user_metadata or {}
    session["display_name"] = metadata.get(
        "display_name", (result.user.email or email).split("@")[0]
    )
    session["animora_level"] = max(1, int(metadata.get("animora_level", 1) or 1))
    session["animora_xp"] = max(0, int(metadata.get("animora_xp", 0) or 0))

    # Восстанавливаем постоянную ссылку на аватар из Supabase Storage
    # при каждом входе, чтобы он отображался на других устройствах.
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if supabase_url:
        session["avatar_url"] = (
            f"{supabase_url}/storage/v1/object/public/avatars/"
            f"{result.user.id}/avatar?v={uuid.uuid4().hex}"
        )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if not email or "@" not in email:
            flash("Введи корректный адрес электронной почты.")
            return redirect(url_for("register"))
        if len(password) < 8:
            flash("Пароль должен содержать минимум 8 символов.")
            return redirect(url_for("register"))
        if password != password_confirm:
            flash("Пароли не совпадают.")
            return redirect(url_for("register"))

        try:
            result = get_supabase().auth.sign_up({
                "email": email,
                "password": password,
                "options": {"data": {"display_name": email.split("@")[0]}}
            })
            establish_auth_session(result, email)
            flash("Аккаунт ANIMORA успешно создан!")
            return redirect(url_for("profile"))
        except Exception as exc:
            app.logger.warning("SUPABASE PASSWORD SIGNUP FAILED: %s", str(exc)[:500])
            message = str(exc).lower()
            if "already registered" in message or "already exists" in message:
                flash("Аккаунт с этой почтой уже существует. Войди в него.")
                return redirect(url_for("login"))
            if "password" in message and ("weak" in message or "short" in message):
                flash("Пароль слишком простой. Используй минимум 8 символов.")
            else:
                flash("Не удалось создать аккаунт. Проверь настройки Email в Supabase и попробуй ещё раз.")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or "@" not in email or not password:
            flash("Введи почту и пароль.")
            return redirect(url_for("login"))

        try:
            result = get_supabase().auth.sign_in_with_password({
                "email": email,
                "password": password
            })
            establish_auth_session(result, email)
            return redirect(url_for("profile"))
        except Exception as exc:
            app.logger.warning("SUPABASE PASSWORD LOGIN FAILED: %s", str(exc)[:500])
            flash("Неверная почта или пароль. Проверь данные и попробуй ещё раз.")

    return render_template("login.html")


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user_id" not in session:
        flash("Сначала войди в аккаунт.")
        return redirect(url_for("login"))

    if request.method == "POST":
        action = request.form.get("action")

        if action == "admin_update_progress":
            if not is_admin_user():
                flash("Доступ запрещён.")
                return redirect(url_for("profile"))

            try:
                level = int(request.form.get("level", "1"))
                xp = int(request.form.get("xp", "0"))
                if not 1 <= level <= 1000000 or not 0 <= xp <= 1000000000:
                    raise ValueError("out of range")

                base_url = os.environ["SUPABASE_URL"].rstrip("/")
                headers = supabase_headers()
                user_response = requests.get(
                    f"{base_url}/auth/v1/user",
                    headers=headers,
                    timeout=20
                )
                user_response.raise_for_status()
                user_data = user_response.json()
                metadata = user_data.get("user_metadata") or {}
                metadata.update({
                    "animora_level": level,
                    "animora_xp": xp
                })
                update_response = requests.put(
                    f"{base_url}/auth/v1/user",
                    headers=headers,
                    json={"data": metadata},
                    timeout=20
                )
                update_response.raise_for_status()
                session["animora_level"] = level
                session["animora_xp"] = xp
                flash(f"Прогресс обновлён: уровень {level}, {xp} XP.")
            except (TypeError, ValueError):
                flash("Укажи корректные числа: уровень от 1 до 1 000 000, XP от 0 до 1 000 000 000.")
            except Exception:
                app.logger.exception("ADMIN PROGRESS UPDATE ERROR")
                flash("Не удалось сохранить прогресс. Попробуй войти заново.")

            return redirect(url_for("profile"))

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
        watchlist = get_watchlist()
    except Exception:
        app.logger.exception("WATCHLIST LOAD ERROR")
        watchlist = []

    return render_template(
        "profile.html",
        email=session.get("user_email"),
        avatar_url=session.get("avatar_url"),
        display_name=session.get("display_name", "Пользователь"),
        favorites=favorites,
        watchlist=watchlist,
        is_admin=is_admin_user(),
        viewer_level=session.get("animora_level", 1),
        viewer_xp=session.get("animora_xp", 0)
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


@app.route("/watchlist/list")
def watchlist_list():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Сначала войди в аккаунт."}), 401
    try:
        return jsonify({"success": True, "items": get_watchlist()})
    except Exception:
        app.logger.exception("WATCHLIST LIST ERROR")
        return jsonify({"success": False, "message": "Не удалось загрузить список."}), 500

@app.route("/watchlist/toggle", methods=["POST"])
def watchlist_toggle():
    if "user_id" not in session or "access_token" not in session:
        return jsonify({"success": False, "message": "Сначала войди в аккаунт."}), 401
    data = request.get_json(silent=True) or {}
    anime_id = str(data.get("anime_id", "")).strip()
    title = str(data.get("title", "")).strip()[:300]
    poster = str(data.get("poster", "")).strip()[:2000]
    genre = str(data.get("genre", "")).strip()[:300]
    status = str(data.get("status", "")).strip()
    if not anime_id or not title or status not in {"watching", "planned", "watched", "dropped", "favorite"}:
        return jsonify({"success": False, "message": "Проверь аниме и статус."}), 400
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/animora_watchlist"
    headers = supabase_headers()
    try:
        check = requests.get(url, headers=headers, params={"select": "status", "user_id": f"eq.{session['user_id']}", "anime_id": f"eq.{anime_id}"}, timeout=30)
        check.raise_for_status()
        existing = check.json()
        if existing and existing[0].get('status') == status:
            response = requests.delete(url, headers=headers, params={"user_id": f"eq.{session['user_id']}", "anime_id": f"eq.{anime_id}"}, timeout=30)
            response.raise_for_status()
            return jsonify({"success": True, "active": False, "message": "Статус убран."})
        payload = {"user_id": session["user_id"], "anime_id": anime_id, "title": title, "poster": poster, "genre": genre, "status": status}
        response = requests.post(url, headers={**headers, "Prefer": "resolution=merge-duplicates,return=representation"}, params={"on_conflict": "user_id,anime_id"}, json=payload, timeout=30)
        response.raise_for_status()
        return jsonify({"success": True, "active": True, "message": "Статус сохранён."})
    except Exception:
        app.logger.exception("WATCHLIST TOGGLE ERROR")
        return jsonify({"success": False, "message": "Не удалось сохранить статус. Проверь таблицу animora_watchlist в Supabase."}), 502

@app.route("/logout")
def logout():
    session.clear()
    flash("Ты вышел из аккаунта.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
