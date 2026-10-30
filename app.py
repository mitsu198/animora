
import os

from flask import (
    Flask, render_template, request,
    redirect, url_for, session, flash,
    send_from_directory
)
from supabase import create_client

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")


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
            supabase = get_supabase()
            result = supabase.auth.sign_up({
                "email": email,
                "password": password
            })

            if result.session:
                session["user_email"] = result.user.email
                session["user_id"] = result.user.id
                return redirect(url_for("profile"))

            flash("Проверь почту и подтверди регистрацию, затем войди.")
            return redirect(url_for("login"))

        except Exception:
            app.logger.exception("SUPABASE REGISTRATION ERROR")
            flash("Не удалось зарегистрироваться. Проверь настройки и попробуй снова.")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        try:
            supabase = get_supabase()
            result = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })

            session["user_email"] = result.user.email
            session["user_id"] = result.user.id

            return redirect(url_for("profile"))

        except Exception:
            app.logger.exception("SUPABASE LOGIN ERROR")
            flash("Не удалось войти. Проверь почту и пароль.")

    return render_template("login.html")


@app.route("/profile")
def profile():
    if "user_id" not in session:
        flash("Сначала войди в аккаунт.")
        return redirect(url_for("login"))

    return render_template(
        "profile.html",
        email=session.get("user_email")
    )


@app.route("/logout")
def logout():
    session.clear()
    flash("Ты вышел из аккаунта.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
