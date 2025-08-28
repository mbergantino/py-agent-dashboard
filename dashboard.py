from flask import Flask, render_template, request, redirect, url_for, flash, Response
from collections import deque
from crontab import CronTab
from datetime import datetime, timezone
import os,re
from pathlib import Path

app = Flask(__name__)
app.secret_key = "pi-dashboard"
user_cron = CronTab(user=True)

BASE_DIR    = Path(__file__).resolve().parent
SCRIPTS_DIR = (BASE_DIR / "scripts").resolve()
LOGS_DIR    = (BASE_DIR / "logs").resolve()
RUNNER      = f"/usr/bin/python3 {BASE_DIR}/runner.py"
BOOT_SYNC_DONE = False

# Make directory structures needed
SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

SAMPLE_NAME = "helloworld"
SAMPLE_SCRIPT="""
from datetime import datetime
print(f"{datetime.now()} Hello from the Raspberry Pi Dashboard!")
"""

print(f" * Will write new scripts to {SCRIPTS_DIR} and logs to {LOGS_DIR}")

@app.before_request
def _boot_sync_once():
    global BOOT_SYNC_DONE
    if not BOOT_SYNC_DONE:
        rebuild_jobs()      # ensure jobs list reflects scripts + crontab
        BOOT_SYNC_DONE = True

@app.route("/")
def index():
    jobs = rebuild_jobs()
    return render_template("index.html", jobs=jobs)

@app.route("/view/<name>")
def view(name):
    log_file = os.path.join(LOGS_DIR, f"{name}.log")
    content = open(log_file).read() if os.path.exists(log_file) else "No logs found."
    return render_template("view.html", name=name, content=content)

@app.route("/purge/<name>")
def purge_log(name):
    log_file = os.path.join(LOGS_DIR, f"{name}.log")

    if os.path.exists(log_file): 
        with open(log_file, 'w') as file:
            pass
    return redirect(url_for("index"))

@app.route("/run/<name>")
def run_now(name):
    script_path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    log_path = os.path.join(LOGS_DIR, f"{name}.log")
    os.system(f"{RUNNER} {script_path} >> {log_path} 2>&1 &")
    #os.system(f"/usr/bin/python3 {script_path} >> {log_path} 2>&1 &")
    flash(f"{name}.py launched manually", "info")
    return redirect(url_for("index"))

@app.route("/edit/<name>", methods=["GET", "POST"])
def edit(name):
    script_path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    job = next((j for j in user_cron if j.comment == name), None)

    if request.method == "POST":
        new_content = request.form["script"]
        schedule = request.form.get("schedule", "").strip()

        # Save script contents
        with open(script_path, "w") as f:
            f.write(new_content)

        # Handle "DISABLED" = remove cron job if present
        if schedule.upper() == "DISABLED":
            if job:
                user_cron.remove(job)
                user_cron.write()
            flash(f"{name}.py saved; schedule is DISABLED", "warning")
            return redirect(url_for("index"))

        # Otherwise, (re)create cron job with the provided expression
        try:
            if job:
                user_cron.remove(job)
            new_job = user_cron.new(
                command=f"{RUNNER} {script_path} >> {LOGS_DIR}/{name}.log 2>&1",
                #command=f"/usr/bin/python3 {script_path} >> {LOGS_DIR}/{name}.log 2>&1",
                comment=name
            )
            new_job.setall(schedule)  # raises if invalid
            user_cron.write()
            flash(f"{name}.py saved; schedule set to '{schedule}'", "success")
        except Exception as e:
            flash(f"Invalid cron schedule: '{schedule}' ({e})", "danger")
        return redirect(url_for("index"))

    # GET: render editor
    current_schedule = (job.slices if job else "DISABLED")
    content = open(script_path).read() if os.path.exists(script_path) else ""
    return render_template("edit.html", name=name, content=content, schedule=current_schedule)

@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "POST":
        raw_name = request.form["name"].strip()
        name = re.sub(r"[^A-Za-z0-9_\-]", "_", raw_name)  # sanitize
        if name.endswith(".py"):  # avoid double .py
            name = name[:-3]

        script_path = os.path.join(SCRIPTS_DIR, f"{name}.py")

        if os.path.exists(script_path):
            # return the page with an error and preserve input
            error = f"A script named '{name}.py' already exists."
            return render_template("add.html", error=error, name=raw_name), 409

        # (optional) create the file now so the name is reserved
        open(script_path, "a").close()
        return redirect(url_for("edit", name=name))

    return render_template("add.html")

@app.route("/add-sample", methods=["POST"])
def add_sample():
    name = SAMPLE_NAME
    script_path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    log_path = os.path.join(LOGS_DIR, f"{name}.log")

    # Create script if missing (do not overwrite existing)
    created = False
    if not os.path.exists(script_path):
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_SCRIPT)
        os.chmod(script_path, 0o755)
        created = True
    
    if created:
        flash("Sample 'helloworld' script created.", "success")
    else:
        flash("Script 'helloworld' already existed; didn't touch it.", "info")

    return redirect(url_for("index"))

@app.route("/delete/<name>")
def delete(name):
    script_path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    log_path = os.path.join(LOGS_DIR, f"{name}.log")
    
    if os.path.exists(script_path):
        os.remove(script_path)
    
    if os.path.exists(log_path):
        os.remove(log_path)

    for job in user_cron:
        if job.comment == name:
            user_cron.remove(job)
    user_cron.write()
    flash(f"{name}.py and its cron job deleted", "warning")
    return redirect(url_for("index"))

@app.get("/api/log/<name>")
def api_log(name):
    """Return last N lines of the log as plain text (no cache)."""
    log_path = os.path.join(LOGS_DIR, f"{name}.log")
    try:
        lines = int(request.args.get("lines", 500))
    except ValueError:
        lines = 500

    if not os.path.exists(log_path):
        return Response("", mimetype="text/plain", headers={"Cache-Control": "no-store"})

    # Efficient tail of last N lines
    with open(log_path, "rb") as f:
        tail_bytes = b"".join(deque(f, maxlen=lines))
    text = tail_bytes.decode("utf-8", errors="replace")
    return Response(text, mimetype="text/plain", headers={"Cache-Control": "no-store"})

def _fmt_mtime(p: Path):
    if p.exists():
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return None

def last_modified_epoch(filepath: Path):
    try:
        return int(os.path.getmtime(filepath))
    except FileNotFoundError:
        return None

def _name_from_cron(job):
    # Prefer the comment as canonical name
    if job.comment:
        return job.comment
    # Otherwise try to extract from the script path in the command
    for tok in job.command.split():
        if tok.endswith(".py") and str(SCRIPTS_DIR) in tok:
            return Path(tok).stem
    return None

def rebuild_jobs():
    """
    Returns a list of job dicts by reconciling *.py files in scripts/
    with cron entries that invoke them.
    Each dict has: name, script_path, has_script, has_cron, schedule, last_run, log_path
    """
    jobs = {}

    # 1) Start with every script on disk
    for script in sorted(SCRIPTS_DIR.glob("*.py")):
        name = script.stem
        log_path = LOGS_DIR / f"{name}.log"
        jobs[name] = {
            "name": name,
            "script_path": str(script),
            "has_script": True,
            "has_cron": False,
            "schedule": None,
            "last_run": _fmt_mtime(log_path),
            "last_run_epoch": last_modified_epoch(log_path),
            "log_path": str(log_path)
        }

    # 2) Merge in cron entries that reference scripts under SCRIPTS_DIR
    for job in user_cron:
        if str(SCRIPTS_DIR) in job.command:
            name = _name_from_cron(job)
            if not name:
                continue
            if name not in jobs:
                # Cron points to a script that doesn't exist anymore (or lives elsewhere)
                script_in_cmd = None
                for tok in job.command.split():
                    if tok.endswith(".py"):
                        script_in_cmd = tok
                        break
                log_path = LOGS_DIR / f"{name}.log"
                jobs[name] = {
                    "name": name,
                    "script_path": script_in_cmd or "",
                    "has_script": False,
                    "has_cron": True,
                    "schedule": str(job.slices),
                    "last_run": _fmt_mtime(log_path),
                    "last_run_epoch": last_modified_epoch(log_path),
                    "log_path": str(log_path)
                }
            else:
                jobs[name]["has_cron"] = True
                jobs[name]["schedule"] = str(job.slices)

    # Stable order for table rendering
    return [jobs[k] for k in sorted(jobs.keys())]

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
