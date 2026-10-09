from datetime import datetime
from flask import Flask, render_template

app = Flask(__name__)


# Define the URL route (Root address)
@app.route("/")
def home():
    # 1. Generate or fetch server-side data
    name = "Alex"
    server_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 2. Render the HTML, passing the variables to the template
    return render_template(
        "index.html", user_name=name, current_time=server_time
    )


if __name__ == "__main__":
    # Run a local development server
    app.run(debug=True)
