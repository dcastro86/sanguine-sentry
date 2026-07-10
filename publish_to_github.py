import os
import sys
import json
import subprocess
import urllib.request
import urllib.error

def load_token():
    # Attempt to load GITHUB_TOKEN from ~/.env or .env in the project directory
    env_paths = [
        os.path.expanduser("~/.env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    ]
    for path in env_paths:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GITHUB_TOKEN="):
                        return line.split("=", 1)[1].strip('"').strip("'")
    return None

def github_api_request(url, token, data=None, method="GET"):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "SanguineSentryPublisher")
    
    if data is not None:
        req.add_header("Content-Type", "application/json")
        json_data = json.dumps(data).encode("utf-8")
    else:
        json_data = None

    try:
        with urllib.request.urlopen(req, data=json_data) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_data = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_data = e.reason
        return e.code, err_data
    except Exception as e:
        return 0, str(e)

def run_git_command(args, check=True):
    print(f"Running: git {' '.join(args)}")
    res = subprocess.run(["git"] + args, capture_output=True, text=True)
    if check and res.returncode != 0:
        print(f"Git command failed: git {' '.join(args)}")
        print(f"Stdout:\n{res.stdout}")
        print(f"Stderr:\n{res.stderr}")
        sys.exit(res.returncode)
    return res

def main():
    print("Starting GitHub publisher helper...")
    token = load_token()
    if not token:
        print("ERROR: GITHUB_TOKEN not found in ~/.env or project .env file.")
        print("Please configure GITHUB_TOKEN first using the secure command.")
        sys.exit(1)

    print("Verifying GitHub token and fetching user details...")
    status, user_info = github_api_request("https://api.github.com/user", token)
    if status != 200:
        print(f"ERROR: Failed to authenticate with GitHub (HTTP {status}).")
        print(f"Details: {user_info}")
        sys.exit(1)

    username = user_info.get("login")
    name = user_info.get("name") or username
    user_id = user_info.get("id")
    email = user_info.get("email") or f"{user_id}+{username}@users.noreply.github.com"

    print(f"Authenticated successfully as {username} ({name}).")

    repo_name = "sanguine-sentry"
    print(f"Creating repository '{repo_name}' on GitHub...")
    repo_data = {
        "name": repo_name,
        "private": True,
        "description": "Auto-Flask calibration and health globe monitoring dashboard for Action RPGs"
    }
    
    status, repo_info = github_api_request("https://api.github.com/user/repos", token, data=repo_data, method="POST")
    if status == 201:
        print(f"Successfully created GitHub repository: {repo_info.get('html_url')}")
    elif status == 422 and isinstance(repo_info, dict) and any("already exists" in err.get("message", "") for err in repo_info.get("errors", [])):
        print(f"Repository '{repo_name}' already exists on GitHub. Proceeding with upload.")
    else:
        print(f"ERROR: Failed to create repository (HTTP {status}).")
        print(f"Details: {repo_info}")
        sys.exit(1)

    print("Configuring local Git repository...")
    run_git_command(["init"])
    run_git_command(["config", "user.name", name])
    run_git_command(["config", "user.email", email])
    run_git_command(["branch", "-m", "main"])

    print("Staging files...")
    run_git_command(["add", "."])

    # Check if there are changes to commit
    status_res = run_git_command(["status", "--porcelain"])
    if not status_res.stdout.strip():
        print("No changes to commit. Repository is clean.")
    else:
        print("Committing files...")
        # Ignore returncode in commit if it's already committed
        run_git_command(["commit", "-m", "Initial commit: Add Sanguine Sentry app and dashboard"], check=False)

    print("Configuring remote repository...")
    # Remove origin if it already exists
    run_git_command(["remote", "remove", "origin"], check=False)
    
    remote_url = f"https://{token}@github.com/{username}/{repo_name}.git"
    run_git_command(["remote", "add", "origin", remote_url])

    print("Pushing codebase to GitHub main branch...")
    # We suppress token leaks in the stdout/stderr of push by running carefully
    res = subprocess.run(["git", "push", "-u", "origin", "main"], capture_output=True, text=True)
    if res.returncode != 0:
        print("ERROR: Git push failed.")
        # Filter token from output to keep logs safe
        filtered_stdout = res.stdout.replace(token, "********")
        filtered_stderr = res.stderr.replace(token, "********")
        print(f"Stdout:\n{filtered_stdout}")
        print(f"Stderr:\n{filtered_stderr}")
        sys.exit(res.returncode)

    print("Push completed successfully!")
    print(f"Repository is available at: https://github.com/{username}/{repo_name}")

if __name__ == "__main__":
    main()
