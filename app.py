
import os
import uuid
import requests

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify
)
from supabase import create_client

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


def get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    if not url or not key:
        raise RuntimeError("Настройки Supabase не найдены")

    return create_client(url, key)


def supabase_headers():
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
    return render_template("index.html")


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
        favorites=favorites
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

    except Exception:
        app.logger.exception("FAVORITE TOGGLE ERROR")
        return jsonify({
            "success": False,
            "message": "Не удалось обновить избранное."
        }), 500


@app.route("/logout")
def logout():
    session.clear()
    flash("Ты вышел из аккаунта.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
