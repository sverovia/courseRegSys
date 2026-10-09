from datetime import datetime
from flask import Flask, render_template, request

app = Flask(__name__, template_folder="src")


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

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        # Extract the input values using the 'name' attribute from the HTML
        student_name = request.form.get("student_name")
        course_selected = request.form.get("course")

        # Process the data (e.g., save to a database, calculate logic)
        message = f"Success! {student_name} registered for {course_selected}."

        # Send the user to a confirmation state, passing the success message
        return render_template(
            "register.html", status_message=message, is_submitted=True
        )
    else:
        return render_template("register.html")

if __name__ == "__main__":
    # Run a local development server
    app.run(debug=True)
