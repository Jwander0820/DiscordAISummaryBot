from flask import Flask
app = Flask(__name__)
@app.route("/")
def keep_alive():
    return "I'm alive!", 200
