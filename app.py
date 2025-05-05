import os
from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is alive!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # 如果沒有 PORT 就用 5000
    app.run(host="0.0.0.0", port=port)
