from flask import Flask, render_template, send_from_directory

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/anime")
def anime():
    return send_from_directory(".", "anime.html")

if __name__ == "__main__":
    app.run(debug=True)
