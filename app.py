import os
from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is alive!", 200

@app.get("/health")
def health():
    return jsonify(ok=True), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))  # 如果沒有 PORT 就用 5000
    app.run(host="0.0.0.0", port=port)
