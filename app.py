import os
import uuid
import requests

from flask import (
    Flask, render_template, request,
    redirect, url_for, session, flash
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


@app.route("/")
def index():
    return render_template("index.html")


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

        # Изменение имени пользователя
        if action == "update_name":
            display_name = request.form.get("display_name", "").strip()

            if len(display_name) < 2 or len(display_name) > 24:
                flash("Имя должно содержать от 2 до 24 символов.")
                return redirect(url_for("profile"))

            try:
                base_url = os.environ["SUPABASE_URL"].rstrip("/")
                api_key = os.environ["SUPABASE_KEY"]
                token = session["access_token"]

                response = requests.patch(
                    f"{base_url}/auth/v1/user",
                    headers={
                        "apikey": api_key,
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "data": {
                            "display_name": display_name
                        }
                    },
                    timeout=30
                )

                if not response.ok:
                    raise RuntimeError(
                        f"Supabase: {response.status_code} {response.text}"
                    )

                session["display_name"] = display_name
                flash("Имя пользователя успешно изменено!")

            except Exception:
                app.logger.exception("PROFILE NAME UPDATE ERROR")
                flash("Не удалось изменить имя. Попробуй войти заново.")

            return redirect(url_for("profile"))

        # Загрузка аватара
        if action == "upload_avatar":
            image = request.files.get("avatar")

            if not image or not image.filename:
                flash("Сначала выбери изображение.")
                return redirect(url_for("profile"))

            allowed_types = {
                "image/jpeg": "jpg",
                "image/png": "png",
                "image/webp": "webp",
                "image/gif": "gif"
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
                api_key = os.environ["SUPABASE_KEY"]
                user_id = session["user_id"]
                token = session["access_token"]

                file_url = (
                    f"{base_url}/storage/v1/object/avatars/"
                    f"{user_id}/avatar"
                )

                response = requests.post(
                    file_url,
                    headers={
                        "apikey": api_key,
                        "Authorization": f"Bearer {token}",
                        "Content-Type": image.mimetype,
                        "x-upsert": "true"
                    },
                    data=image_data,
                    timeout=30
                )

                if not response.ok:
                    raise RuntimeError(
                        f"Supabase: {response.status_code} {response.text}"
                    )

                session["avatar_url"] = (
                    f"{base_url}/storage/v1/object/public/avatars/"
                    f"{user_id}/avatar?v={uuid.uuid4().hex}"
                )

                flash("Аватар успешно обновлён!")

            except Exception:
                app.logger.exception("AVATAR UPLOAD ERROR")
                flash("Не удалось загрузить аватар. Попробуй снова.")

            return redirect(url_for("profile"))

    return render_template(
        "profile.html",
        email=session.get("user_email"),
        avatar_url=session.get("avatar_url"),
        display_name=session.get("display_name", "Пользователь")
    )


@app.route("/logout")
def logout():
    session.clear()
    flash("Ты вышел из аккаунта.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
