import subprocess


def configure_git(name, email):
    try:
        # Set Git user.name
        subprocess.run(
            ["git", "config", "--global", "user.name", name], check=True
        )
        print(f"Successfully set user.name to: {name}")

        # Set Git user.email
        subprocess.run(
            ["git", "config", "--global", "user.email", email], check=True
        )
        print(f"Successfully set user.email to: {email}")

        print("\nYour Git identity has been updated successfully!")

    except subprocess.CalledProcessError as e:
        print(f"Error executing Git command: {e}")
    except FileNotFoundError:
        print(
            "Git is not installed or not found in system PATH. Please install Git first."
        )


if __name__ == "__main__":
    # Your details configured:
    USER_NAME = "Akbar Shah"  # Change this to your preferred full name
    USER_EMAIL = "m.akbarshah1974@gmail.com"

    configure_git(USER_NAME, USER_EMAIL)