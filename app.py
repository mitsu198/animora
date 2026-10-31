
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

    return render_template(
        "profile.html",
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
