from flask import (Flask, render_template, request, redirect, url_for,
                   flash, Response, abort, jsonify)
from collections import deque
from crontab import CronTab
from datetime import datetime, timezone
import os, re, sys, subprocess, time, json, platform, shutil, plistlib, tempfile
from pathlib import Path

app = Flask(__name__)
app.secret_key = "pi-dashboard"
app.config.update(
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

BASE_DIR    = Path(__file__).resolve().parent
JOBS_DIR    = (BASE_DIR / "jobs").resolve()
LOGS_DIR    = (BASE_DIR / "logs").resolve()
CONFIG_DIR  = (BASE_DIR / "config").resolve()
BOOT_SYNC_DONE = False
_running_procs: dict = {}  # slug -> Popen

IS_MAC     = (platform.system() == "Darwin")
IS_LINUX   = (platform.system() == "Linux")
IS_WINDOWS = (platform.system() == "Windows")

PYTHON_EXE    = sys.executable
RUNNER_SCRIPT = str(BASE_DIR / "runner.py")

SAFE_UNIT        = re.compile(r"^[\w\-.@]+$")
SERVICE_CACHE    = {"expires": 0.0, "items": []}
SERVICE_CACHE_TTL = 30.0

RUN_WAIT_MAX  = 8.0
RUN_WAIT_STEP = 0.25

JOBS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

SAMPLE_NAME   = "helloworld"
SAMPLE_SCRIPT = """from datetime import datetime
print(f"{datetime.now()} Hello from the Script Dashboard!")
"""

print(f" * Jobs directory: {JOBS_DIR}")
print(f" * Platform: {platform.system()} | Python: {PYTHON_EXE}")

# ── Config helpers ────────────────────────────────────────────────────────────

def _cfg_path(name): return CONFIG_DIR / f"{name}.json"

def load_cfg(name):
    p = _cfg_path(name)
    if p.exists():
        try: return json.load(open(p, "r", encoding="utf-8"))
        except Exception: return {}
    return {}

def save_cfg(name, data: dict):
    _cfg_path(name).parent.mkdir(parents=True, exist_ok=True)
    with open(_cfg_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

# ── Job directory helpers ─────────────────────────────────────────────────────

def _job_dir(slug: str) -> Path:
    return JOBS_DIR / slug

def _job_file(slug: str, filename: str) -> Path:
    return JOBS_DIR / slug / filename

def _job_files_ordered(slug: str) -> list:
    job_dir = _job_dir(slug)
    if not job_dir.exists():
        return []
    existing = {f.name for f in job_dir.iterdir() if f.is_file()}
    cfg = load_cfg(slug)
    order = cfg.get("file_order", [])
    ordered = [f for f in order if f in existing]
    for f in sorted(existing):
        if f not in ordered:
            ordered.append(f)
    return ordered

def _active_file(slug: str) -> str:
    cfg = load_cfg(slug)
    af = cfg.get("active_file", "")
    if af and (_job_dir(slug) / af).exists():
        return af
    files = _job_files_ordered(slug)
    return files[0] if files else ""

def _safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")

# ── Encryption helpers ────────────────────────────────────────────────────────

def _get_fernet():
    key_path = CONFIG_DIR / ".key"
    try:
        from cryptography.fernet import Fernet
        if key_path.exists():
            key = key_path.read_bytes()
        else:
            key = Fernet.generate_key()
            key_path.write_bytes(key)
        return Fernet(key)
    except ImportError:
        return None
    except Exception:
        return None

def encrypt_token(token: str) -> str:
    f = _get_fernet()
    if f is None:
        return token
    return f.encrypt(token.encode()).decode()

# ── Timezone helpers ──────────────────────────────────────────────────────────

def _system_timezone_name() -> str:
    if IS_WINDOWS:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation")
            tz_id, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
            winreg.CloseKey(key)
            if tz_id: return tz_id
        except Exception:
            pass
        return time.tzname[0] if time.tzname and time.tzname[0] else "UTC"
    try:
        link = os.path.realpath("/etc/localtime")
        marker = "zoneinfo/"
        idx = link.find(marker)
        if idx != -1:
            return link[idx + len(marker):]
    except Exception:
        pass
    return time.tzname[0] if time.tzname and time.tzname[0] else "UTC"

SYSTEM_TZ_NAME = _system_timezone_name()

def _apply_cron_timezone(user_cron):
    if IS_WINDOWS: return
    try:
        if user_cron.env.get("CRON_TZ") != SYSTEM_TZ_NAME:
            user_cron.env["CRON_TZ"] = SYSTEM_TZ_NAME
            user_cron.write()
    except Exception as e:
        print(f" ! could not set CRON_TZ in crontab: {e}")

@app.before_request
def _boot_sync_once():
    global BOOT_SYNC_DONE
    if not BOOT_SYNC_DONE:
        if not IS_WINDOWS:
            _apply_cron_timezone(CronTab(user=True))
        rebuild_jobs()
        BOOT_SYNC_DONE = True

# ── Index ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    jobs = rebuild_jobs()
    has_scheduled = any(j.get("has_cron") for j in jobs)
    return render_template("index.html", jobs=jobs, has_scheduled=has_scheduled)

@app.route("/add", methods=["GET", "POST"])
def add():
    return redirect(url_for("edit_new"))

# ── New job ───────────────────────────────────────────────────────────────────

@app.route("/edit/", methods=["GET", "POST"])
def edit_new():
    if request.method == "POST":
        raw_name = request.form.get("job_display_name", "").strip()
        if not raw_name:
            flash("Job name is required.", "danger")
            return render_template("edit.html", is_new=True, name="", display_name="",
                                   files=[], active_file="", exec_command="",
                                   has_ai_file=False, schedule="DISABLED",
                                   tzname=SYSTEM_TZ_NAME, run_until_success=False)

        slug = re.sub(r"[^A-Za-z0-9_\-]", "_", raw_name)
        if slug.endswith(".py"):
            slug = slug[:-3]
        if not slug:
            flash("Job name must contain at least one valid character.", "danger")
            return render_template("edit.html", is_new=True, name="", display_name="",
                                   files=[], active_file="", exec_command="",
                                   has_ai_file=False, schedule="DISABLED",
                                   tzname=SYSTEM_TZ_NAME, run_until_success=False)

        job_dir = _job_dir(slug)
        if job_dir.exists():
            flash(f"A job named '{slug}' already exists.", "danger")
            return render_template("edit.html", is_new=True, name="", display_name=raw_name,
                                   files=[], active_file="", exec_command="",
                                   has_ai_file=False, schedule="DISABLED",
                                   tzname=SYSTEM_TZ_NAME, run_until_success=False)

        job_dir.mkdir(parents=True, exist_ok=True)
        schedule = request.form.get("schedule", "").strip()
        exec_command = request.form.get("exec_command", "").strip()
        run_until_success = request.form.get("run_until_success") == "on"
        cfg = {"display_name": raw_name, "run_until_success": run_until_success,
               "exec_command": exec_command, "file_order": [], "active_file": ""}

        if IS_WINDOWS:
            if schedule and schedule.upper() != "DISABLED":
                try:
                    _create_win_task(slug, str(LOGS_DIR / f"{slug}.log"), schedule)
                    cfg["win_schedule"] = schedule
                    flash(f"'{raw_name}' created and scheduled.", "success")
                except Exception as e:
                    flash(f"Created but scheduling failed: {e}", "warning")
            else:
                flash(f"'{raw_name}' created.", "success")
        else:
            if schedule and schedule.upper() != "DISABLED":
                try:
                    user_cron = CronTab(user=True)
                    cron_cmd = (f'RUN_CONTEXT=cron "{PYTHON_EXE}" "{RUNNER_SCRIPT}" --job "{slug}"'
                                f' >> "{LOGS_DIR}/{slug}.log" 2>&1')
                    new_job = user_cron.new(command=cron_cmd, comment=slug)
                    new_job.setall(schedule)
                    _apply_cron_timezone(user_cron)
                    user_cron.write()
                    flash(f"'{raw_name}' created and scheduled.", "success")
                except Exception as e:
                    flash(f"Created but scheduling failed: {e}", "warning")
            else:
                flash(f"'{raw_name}' created.", "success")

        save_cfg(slug, cfg)
        return redirect(url_for("edit", name=slug))

    return render_template("edit.html", is_new=True, name="", display_name="",
                           files=[], active_file="", exec_command="",
                           has_ai_file=False, schedule="DISABLED",
                           tzname=SYSTEM_TZ_NAME, run_until_success=False)

# ── Sample job ────────────────────────────────────────────────────────────────

@app.route("/add-sample", methods=["POST"])
def add_sample():
    slug    = SAMPLE_NAME
    job_dir = _job_dir(slug)
    py_file = job_dir / "helloworld.py"

    created = False
    if not py_file.exists():
        job_dir.mkdir(parents=True, exist_ok=True)
        py_file.write_text(SAMPLE_SCRIPT, encoding="utf-8")
        cfg = load_cfg(slug)
        cfg.update({"display_name": "Hello World", "active_file": "helloworld.py",
                    "file_order": ["helloworld.py"]})
        save_cfg(slug, cfg)
        created = True

    if created:
        flash("Sample 'Hello World' job created.", "success")
    else:
        flash("Sample 'Hello World' already existed; didn't touch it.", "info")
    return redirect(url_for("index"))

# ── Delete job ────────────────────────────────────────────────────────────────

@app.route("/delete/<name>", methods=["POST","DELETE"])
def delete(name):
    job_dir  = _job_dir(name)
    log_file = LOGS_DIR / f"{name}.log"

    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    if log_file.exists():
        log_file.unlink(missing_ok=True)

    if IS_WINDOWS:
        _delete_win_task(name)
    else:
        user_cron = CronTab(user=True)
        for job in list(user_cron):
            if job.comment == name:
                user_cron.remove(job)
        user_cron.write()

    cfgp = _cfg_path(name)
    if cfgp.exists():
        try: cfgp.unlink()
        except Exception: pass

    flash(f"Job '{name}' deleted", "warning")
    return redirect(url_for("index"))

# ── Edit job ──────────────────────────────────────────────────────────────────

@app.route("/edit/<name>", methods=["GET", "POST"])
def edit(name):
    cfg = load_cfg(name)
    job_dir = _job_dir(name)

    if not IS_WINDOWS:
        user_cron = CronTab(user=True)
        cron_job  = next((j for j in user_cron if j.comment == name), None)
    else:
        user_cron = None
        cron_job  = None

    if request.method == "POST":
        schedule         = request.form.get("schedule", "").strip()
        run_until_success = request.form.get("run_until_success") == "on"
        exec_command     = request.form.get("exec_command", "").strip()

        new_cfg = {**cfg, "run_until_success": run_until_success, "exec_command": exec_command}

        if IS_WINDOWS:
            _delete_win_task(name)
            if schedule.upper() == "DISABLED":
                save_cfg(name, {**new_cfg, "win_schedule": None})
                flash(f"{name} saved; schedule DISABLED", "warning")
            else:
                try:
                    _create_win_task(name, str(LOGS_DIR / f"{name}.log"), schedule)
                    save_cfg(name, {**new_cfg, "win_schedule": schedule})
                    flash(f"{name} saved; scheduled ({SYSTEM_TZ_NAME})", "success")
                except Exception as e:
                    save_cfg(name, {**new_cfg, "win_schedule": None})
                    flash(f"Invalid or unsupported schedule: {e}", "danger")
            return redirect(url_for("index"))

        save_cfg(name, new_cfg)
        if schedule.upper() == "DISABLED":
            if cron_job: user_cron.remove(cron_job); user_cron.write()
            flash(f"{name} saved; schedule DISABLED", "warning")
            return redirect(url_for("index"))

        try:
            if cron_job: user_cron.remove(cron_job)
            cron_cmd = (f'RUN_CONTEXT=cron "{PYTHON_EXE}" "{RUNNER_SCRIPT}" --job "{name}"'
                        f' >> "{LOGS_DIR}/{name}.log" 2>&1')
            new_cron_job = user_cron.new(command=cron_cmd, comment=name)
            new_cron_job.setall(schedule)
            _apply_cron_timezone(user_cron)
            user_cron.write()
            flash(f"{name} saved; schedule set to '{schedule}' ({SYSTEM_TZ_NAME})", "success")
        except Exception as e:
            flash(f"Invalid cron schedule: '{schedule}' ({e})", "danger")

        return redirect(url_for("index"))

    if IS_WINDOWS:
        current_schedule = cfg.get("win_schedule") or "DISABLED"
    else:
        current_schedule = (str(cron_job.slices) if cron_job else "DISABLED")

    display_name = cfg.get("display_name") or name.replace("_", " ")
    exec_command = cfg.get("exec_command", "")
    files = _job_files_ordered(name)
    af    = _active_file(name)
    has_ai_file = any(f.lower().endswith(".md") for f in files)

    return render_template("edit.html",
        is_new=False, name=name, display_name=display_name,
        files=files, active_file=af, exec_command=exec_command,
        has_ai_file=has_ai_file,
        schedule=current_schedule, tzname=SYSTEM_TZ_NAME,
        run_until_success=bool(cfg.get("run_until_success", False)))

# ── Purge log ─────────────────────────────────────────────────────────────────

@app.post("/purge/<name>")
def purge_log(name):
    log_file = LOGS_DIR / f"{name}.log"
    if log_file.exists():
        log_file.write_text("", encoding="utf-8")
    return jsonify({"ok": True})

# ── Rename job ────────────────────────────────────────────────────────────────

@app.route('/rename/<name>', methods=['POST'])
def rename_job(name):
    name = name.strip()
    if name.endswith(".py"):
        name = name[:-3]
    display_name = request.form.get("new_name", "").strip()
    if display_name.endswith(".py"):
        display_name = display_name[:-3]
    if display_name and name:
        cfg = load_cfg(name)
        save_cfg(name, {**cfg, "display_name": display_name})
    return redirect(url_for("index"))

# ── Run now ───────────────────────────────────────────────────────────────────

@app.route("/run/<name>", methods=["POST", "GET"])
def run_now(name):
    prev = _log_mtime_epoch(name)
    ok, msg = _launch_job(name)
    if not ok:
        flash(msg, "danger")
        return redirect(url_for("index"))
    deadline = time.time() + RUN_WAIT_MAX
    while time.time() < deadline:
        if _log_mtime_epoch(name) > prev:
            break
        time.sleep(RUN_WAIT_STEP)
    flash(msg, "info")
    return redirect(url_for("index"))

# ── View log ──────────────────────────────────────────────────────────────────

@app.route("/view/<name>")
def view(name):
    cfg          = load_cfg(name)
    log_file     = LOGS_DIR / f"{name}.log"
    display_name = cfg.get("display_name") or name.replace("_", " ")
    content      = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else "No logs found."
    return render_template("view.html", slug=name, name=display_name, content=content)

# ── Launch helper ─────────────────────────────────────────────────────────────

def _log_mtime_epoch(name: str) -> int:
    try:
        return int(os.path.getmtime(LOGS_DIR / f"{name}.log"))
    except FileNotFoundError:
        return 0

def _launch_job(name: str):
    job_dir  = _job_dir(name)
    log_path = LOGS_DIR / f"{name}.log"

    if not job_dir.exists():
        return False, f"Job folder '{name}' not found"

    LOGS_DIR.mkdir(exist_ok=True)

    with open(log_path, "a") as lf:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("RUN_CONTEXT", None)
        proc = subprocess.Popen(
            [PYTHON_EXE, RUNNER_SCRIPT, "--job", name],
            stdout=lf, stderr=subprocess.STDOUT,
            cwd=str(job_dir), close_fds=True, env=env,
        )
    _running_procs[name] = proc
    return True, f"Job '{name}' launched"

# ── File management API ───────────────────────────────────────────────────────

@app.get("/api/files/<slug>")
def api_list_files(slug):
    job_dir = _job_dir(slug)
    if not job_dir.exists():
        return jsonify([])
    files = _job_files_ordered(slug)
    result = []
    for fname in files:
        fp = job_dir / fname
        if fp.exists():
            result.append({
                "name": fname,
                "size": fp.stat().st_size,
                "created": int(fp.stat().st_mtime),
            })
    return jsonify(result)

@app.post("/api/files/<slug>/order")
def api_file_order(slug):
    try:
        order = request.get_json(force=True).get("order", [])
        job_dir = _job_dir(slug)
        existing = {f.name for f in job_dir.iterdir() if f.is_file()} if job_dir.exists() else set()
        order = [f for f in order if f in existing]
        cfg = load_cfg(slug)
        cfg["file_order"] = order
        cfg["active_file"] = order[0] if order else cfg.get("active_file", "")
        save_cfg(slug, cfg)

        if not IS_WINDOWS:
            _maybe_update_cron(slug)

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def _maybe_update_cron(slug: str):
    pass

@app.get("/api/files/<slug>/<path:filename>")
def api_get_file(slug, filename):
    fp = _job_file(slug, filename)
    if not fp.exists():
        return jsonify({"error": "File not found"}), 404

    ext = fp.suffix.lower()

    if ext in (".xlsx", ".xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return jsonify({"type": "xls", "headers": [], "rows": []})
            headers = [str(c) if c is not None else "" for c in rows[0]]
            data = [[str(c) if c is not None else "" for c in row] for row in rows[1:]]
            return jsonify({"type": "xls", "headers": headers, "rows": data})
        except ImportError:
            return jsonify({
                "type": "xls_unavailable",
                "message": "Install openpyxl to view XLS/XLSX files: pip install openpyxl"
            })
        except Exception as e:
            return jsonify({"type": "xls_error", "message": str(e)})

    try:
        content = fp.read_text(encoding="utf-8", errors="replace")
        return Response(content, mimetype="text/plain")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/api/files/<slug>/<path:filename>")
def api_save_file(slug, filename):
    fp = _job_file(slug, filename)
    if not fp.exists():
        return jsonify({"ok": False, "error": "File not found"}), 404
    try:
        data = request.get_json(force=True) or {}
        fp.write_text(data.get("content", ""), encoding="utf-8")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/validate-python/<slug>/<path:filename>")
def api_validate_python(slug, filename):
    try:
        data    = request.get_json(force=True) or {}
        content = data.get("content", "")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", encoding="utf-8",
                                        delete=False) as tf:
            tf.write(content)
            tmp_path = tf.name
        try:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", tmp_path],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return jsonify({"valid": True})
            errors = []
            for line in (result.stderr or result.stdout or "").splitlines():
                m = re.search(r'line (\d+)', line)
                if m:
                    errors.append({"line": int(m.group(1)), "message": line.strip()})
            if not errors and result.stderr:
                errors.append({"line": 0, "message": result.stderr.strip()})
            return jsonify({"valid": False, "errors": errors})
        finally:
            try: os.unlink(tmp_path)
            except Exception: pass
    except Exception as e:
        return jsonify({"valid": False, "errors": [{"line": 0, "message": str(e)}]}), 500

@app.post("/api/files/<slug>/upload")
def api_upload_file(slug):
    job_dir = _job_dir(slug)
    job_dir.mkdir(parents=True, exist_ok=True)
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file"}), 400
    f = request.files["file"]
    fname = _safe_filename(f.filename or "upload")
    if not fname:
        fname = "upload"
    dest = job_dir / fname
    f.save(str(dest))
    cfg = load_cfg(slug)
    order = cfg.get("file_order", [])
    if fname not in order:
        order.append(fname)
    cfg["file_order"] = order
    if not cfg.get("active_file"):
        cfg["active_file"] = fname
    save_cfg(slug, cfg)
    return jsonify({"ok": True, "name": fname})

@app.post("/api/files/<slug>/rename")
def api_rename_file(slug):
    try:
        data    = request.get_json(force=True) or {}
        old     = _safe_filename(data.get("old", ""))
        new     = _safe_filename(data.get("new", ""))
        if not old or not new:
            return jsonify({"ok": False, "error": "Invalid names"}), 400
        src = _job_file(slug, old)
        dst = _job_file(slug, new)
        if not src.exists():
            return jsonify({"ok": False, "error": "Source not found"}), 404
        if dst.exists():
            return jsonify({"ok": False, "error": "Target already exists"}), 409
        src.rename(dst)
        cfg   = load_cfg(slug)
        order = cfg.get("file_order", [])
        if old in order:
            order[order.index(old)] = new
        if cfg.get("active_file") == old:
            cfg["active_file"] = new
        cfg["file_order"] = order
        save_cfg(slug, cfg)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/files/<slug>/clone")
def api_clone_file(slug):
    try:
        data   = request.get_json(force=True) or {}
        source = _safe_filename(data.get("source", ""))
        dest   = _safe_filename(data.get("dest", ""))
        if not source or not dest:
            return jsonify({"ok": False, "error": "Invalid names"}), 400
        src = _job_file(slug, source)
        dst = _job_file(slug, dest)
        if not src.exists():
            return jsonify({"ok": False, "error": "Source not found"}), 404
        if dst.exists():
            return jsonify({"ok": False, "error": "Target already exists"}), 409
        shutil.copy2(src, dst)
        cfg   = load_cfg(slug)
        order = cfg.get("file_order", [])
        if source in order:
            idx = order.index(source)
            order.insert(idx + 1, dest)
        else:
            order.append(dest)
        cfg["file_order"] = order
        save_cfg(slug, cfg)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.delete("/api/files/<slug>/<path:filename>")
def api_delete_file(slug, filename):
    try:
        fp = _job_file(slug, filename)
        if not fp.exists():
            return jsonify({"ok": False, "error": "File not found"}), 404
        fp.unlink()
        cfg   = load_cfg(slug)
        order = cfg.get("file_order", [])
        if filename in order:
            order.remove(filename)
        cfg["file_order"] = order
        if cfg.get("active_file") == filename:
            cfg["active_file"] = order[0] if order else ""
        save_cfg(slug, cfg)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/jobs/<slug>/auth-token")
def api_set_auth_token(slug):
    try:
        data    = request.get_json(force=True) or {}
        env_var = data.get("env_var", "").strip()
        token   = data.get("token", "").strip()
        if not env_var or not token:
            return jsonify({"ok": False, "error": "env_var and token required"}), 400
        encrypted = encrypt_token(token)
        cfg = load_cfg(slug)
        cfg["auth_token_enc"] = encrypted
        cfg["auth_token_env"] = env_var
        save_cfg(slug, cfg)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Existing script save (legacy compat) ──────────────────────────────────────

@app.post("/api/save-script/<name>")
def api_save_script(name):
    af = _active_file(name)
    if not af:
        return jsonify({"ok": False, "error": "No active file"}), 404
    fp = _job_file(name, af)
    try:
        data = request.get_json(force=True) or {}
        fp.write_text(data.get("content", ""), encoding="utf-8")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Log API ───────────────────────────────────────────────────────────────────

@app.get("/api/log/<name>")
def api_log(name):
    log_path = LOGS_DIR / f"{name}.log"
    try:
        lines = int(request.args.get("lines", 500))
    except ValueError:
        lines = 500
    if not log_path.exists():
        return Response("", mimetype="text/plain", headers={"Cache-Control": "no-store"})
    with open(log_path, "rb") as f:
        tail_bytes = b"".join(deque(f, maxlen=lines))
    text = tail_bytes.decode("utf-8", errors="replace")
    return Response(text, mimetype="text/plain", headers={"Cache-Control": "no-store"})

# ── Cron preview / hourly minute ──────────────────────────────────────────────

def _cron_preview(expr: str, now: datetime = None):
    now = now or datetime.now()
    tmp = CronTab()
    job = tmp.new(command="preview")
    job.setall(expr)
    sched = job.schedule(date_from=now)
    return sched.get_prev(), sched.get_next()

@app.get("/api/cron-preview")
def api_cron_preview():
    expr = request.args.get("schedule", "").strip()
    if not expr or expr.upper() == "DISABLED":
        return jsonify({"valid": False, "error": "Schedule is disabled."})
    try:
        prv, nxt = _cron_preview(expr)
        return jsonify({
            "valid": True,
            "prev": prv.strftime("%Y-%m-%d %H:%M:%S"),
            "next": nxt.strftime("%Y-%m-%d %H:%M:%S"),
            "tz": SYSTEM_TZ_NAME,
        })
    except Exception as e:
        return jsonify({"valid": False, "error": f"Invalid cron schedule: {e}"})

HOURLY_SLOT_STEP = 5

def _used_hourly_minutes(exclude_name: str = None):
    used = set()
    if IS_WINDOWS:
        for cfg_file in CONFIG_DIR.glob("*.json"):
            name = cfg_file.stem
            if name.startswith("_"): continue
            if exclude_name and name == exclude_name: continue
            try:
                cfg  = json.load(open(cfg_file, "r", encoding="utf-8"))
                expr = cfg.get("win_schedule")
                if expr:
                    parts = expr.strip().split()
                    if len(parts) == 5 and re.fullmatch(r"\d{1,2}", parts[0]):
                        used.add(int(parts[0]))
            except Exception:
                continue
        return used
    user_cron = CronTab(user=True)
    for job in user_cron:
        if str(JOBS_DIR) not in job.command and str(BASE_DIR / "runner.py") not in job.command:
            continue
        jname = _name_from_cron(job)
        if exclude_name and jname == exclude_name: continue
        try:
            mstr = str(job.minute.render())
            if re.fullmatch(r"\d{1,2}", mstr):
                used.add(int(mstr))
        except Exception:
            continue
    return used

def _next_hourly_minute(exclude_name: str = None) -> int:
    used = _used_hourly_minutes(exclude_name)
    for m in range(0, 60, HOURLY_SLOT_STEP):
        if m not in used: return m
    for m in range(0, 60):
        if m not in used: return m
    return 0

@app.get("/api/next-hourly-minute")
def api_next_hourly_minute():
    exclude = request.args.get("exclude", "").strip() or None
    return jsonify({"minute": _next_hourly_minute(exclude)})

@app.get("/api/running")
def api_running():
    finished = [k for k, p in _running_procs.items() if p.poll() is not None]
    for k in finished:
        del _running_procs[k]
    result = []
    for slug in _running_procs:
        cfg = load_cfg(slug)
        result.append({"slug": slug, "display_name": cfg.get("display_name") or slug.replace("_", " ")})
    return jsonify({"count": len(result), "jobs": result})

# ── Job order (index page drag) ───────────────────────────────────────────────

@app.post("/api/order")
def api_save_order():
    try:
        order = request.get_json(force=True) or []
        (CONFIG_DIR / "_order.json").write_text(
            json.dumps(order, ensure_ascii=False), encoding="utf-8"
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── rebuild_jobs ──────────────────────────────────────────────────────────────

def rebuild_jobs():
    jobs = {}

    for job_dir in sorted(JOBS_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        slug     = job_dir.name
        cfg      = load_cfg(slug)
        log_path = LOGS_DIR / f"{slug}.log"
        af       = _active_file(slug)

        if IS_WINDOWS:
            win_sched = cfg.get("win_schedule")
            has_task  = bool(win_sched) and _win_task_exists(slug)
            schedule  = win_sched if has_task else None
        else:
            has_task = False
            schedule  = None

        jobs[slug] = {
            "name":             slug,
            "display_name":     cfg.get("display_name") or slug.replace("_", " "),
            "script_path":      str(job_dir / af) if af else str(job_dir),
            "has_script":       bool(af),
            "has_cron":         has_task,
            "schedule":         schedule,
            "last_run":         _fmt_mtime(log_path),
            "last_run_epoch":   _last_modified_epoch(log_path),
            "log_path":         str(log_path),
            "run_until_success": bool(cfg.get("run_until_success", False)),
        }

    if not IS_WINDOWS:
        user_cron = CronTab(user=True)
        for job in user_cron:
            if str(RUNNER_SCRIPT) not in job.command:
                continue
            slug = _name_from_cron(job)
            if not slug:
                continue
            if slug not in jobs:
                log_path = LOGS_DIR / f"{slug}.log"
                cfg      = load_cfg(slug)
                jobs[slug] = {
                    "name":             slug,
                    "display_name":     cfg.get("display_name") or slug.replace("_", " "),
                    "script_path":      "",
                    "has_script":       False,
                    "has_cron":         True,
                    "schedule":         str(job.slices),
                    "last_run":         _fmt_mtime(log_path),
                    "last_run_epoch":   _last_modified_epoch(log_path),
                    "log_path":         str(log_path),
                    "run_until_success": bool(cfg.get("run_until_success", False)),
                }
            else:
                jobs[slug]["has_cron"] = True
                jobs[slug]["schedule"] = str(job.slices)

    order_path = CONFIG_DIR / "_order.json"
    try:
        saved_order = json.loads(order_path.read_text(encoding="utf-8"))
    except Exception:
        saved_order = []
    ordered  = [k for k in saved_order if k in jobs]
    ordered += sorted(k for k in jobs if k not in ordered)
    return [jobs[k] for k in ordered]

# ── Misc helpers ──────────────────────────────────────────────────────────────

def _fmt_mtime(p: Path):
    if p.exists():
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return None

def _last_modified_epoch(p: Path):
    try: return int(os.path.getmtime(p))
    except FileNotFoundError: return None

def _name_from_cron(job):
    if job.comment:
        return job.comment
    for tok in job.command.split():
        tok = tok.strip('"')
        if tok.endswith(".py") and (str(JOBS_DIR) in tok or "scripts" in tok):
            return Path(tok).stem
    parts = job.command.split()
    for i, p in enumerate(parts):
        if p == "--job" and i + 1 < len(parts):
            return parts[i + 1].strip('"')
    return None

# ── Windows Task Scheduler ────────────────────────────────────────────────────

def _win_task_name(name: str) -> str:
    return f"PiDashboard\\{name}"

def _win_bat_path(name: str) -> Path:
    return CONFIG_DIR / f"{name}_cron.bat"

def _cron_to_schtasks_args(expr: str) -> list:
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError("Expected 5-field cron expression")
    minute, hour, dom, month, dow = parts
    plain = lambda f: bool(re.fullmatch(r"\d+", f))
    star  = lambda f: f == "*"
    step  = lambda f: re.fullmatch(r"\*/(\d+)", f)
    if expr.strip() == "* * * * *":
        return ["/SC", "MINUTE", "/MO", "1"]
    m = step(minute)
    if m and star(hour) and star(dom) and star(month) and star(dow):
        return ["/SC", "MINUTE", "/MO", m.group(1)]
    if plain(minute) and star(hour) and star(dom) and star(month) and star(dow):
        return ["/SC", "HOURLY", "/MO", "1", "/ST", f"00:{minute.zfill(2)}:00"]
    if plain(minute) and plain(hour) and star(dom) and star(month) and star(dow):
        return ["/SC", "DAILY", "/MO", "1", "/ST", f"{hour.zfill(2)}:{minute.zfill(2)}:00"]
    if plain(minute) and plain(hour) and star(dom) and star(month) and plain(dow):
        _days = {"0":"SUN","1":"MON","2":"TUE","3":"WED","4":"THU","5":"FRI","6":"SAT","7":"SUN"}
        day = _days.get(dow)
        if day:
            return ["/SC", "WEEKLY", "/D", day, "/ST", f"{hour.zfill(2)}:{minute.zfill(2)}:00"]
    if plain(minute) and plain(hour) and plain(dom) and star(month) and star(dow):
        return ["/SC", "MONTHLY", "/D", dom, "/ST", f"{hour.zfill(2)}:{minute.zfill(2)}:00"]
    raise ValueError(f"Cannot map {expr!r} to Windows Task Scheduler.")

def _create_win_task(name: str, log_path: str, cron_expr: str):
    bat = _win_bat_path(name)
    bat.write_text(
        f"@echo off\r\nset RUN_CONTEXT=cron\r\n"
        f'"{PYTHON_EXE}" "{RUNNER_SCRIPT}" --job "{name}" >> "{log_path}" 2>&1\r\n',
        encoding="utf-8"
    )
    args = (["schtasks", "/Create", "/F",
             "/TN", _win_task_name(name),
             "/TR", f'cmd /C "{bat}"']
            + _cron_to_schtasks_args(cron_expr))
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        try: bat.unlink()
        except Exception: pass
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "schtasks /Create failed")

def _delete_win_task(name: str):
    subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", _win_task_name(name)],
        capture_output=True, text=True
    )
    bat = _win_bat_path(name)
    try:
        if bat.exists(): bat.unlink()
    except Exception:
        pass

def _win_task_exists(name: str) -> bool:
    r = subprocess.run(
        ["schtasks", "/Query", "/TN", _win_task_name(name)],
        capture_output=True, text=True
    )
    return r.returncode == 0

# ── Service management (Linux/macOS/Windows) ──────────────────────────────────

def _systemctl(*args):
    proc = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr

def _unit_exists_linux(unit: str) -> bool:
    rc, _, _ = _systemctl("status", unit, "--no-pager")
    return rc == 0

def _owns_unit_linux(unit: str) -> bool:
    if not SAFE_UNIT.match(unit): return False
    rc, out, _ = _systemctl("show", unit, "-p", "WorkingDirectory", "-p", "ExecStart")
    if rc != 0: return False
    wd = es = ""
    for line in out.splitlines():
        if line.startswith("WorkingDirectory="): wd = line.split("=", 1)[1].strip()
        elif line.startswith("ExecStart="):       es = line.split("=", 1)[1].strip()
    base = str(BASE_DIR)
    return (wd and base in wd) or (es and base in es)

def _list_related_services_linux():
    rc, out, _ = _systemctl("list-units", "--type=service", "--all", "--no-legend", "--no-pager")
    if rc != 0: return []
    units = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if not parts: continue
        unit = parts[0]
        if unit.endswith(".service") and _owns_unit_linux(unit):
            units.append(unit)
    for extra in ("flask-dashboard.service", "crowdstrike-watch.service"):
        if extra not in units and _unit_exists_linux(extra) and _owns_unit_linux(extra):
            units.append(extra)
    return sorted(set(units))

def _service_log_linux(unit: str, lines: int) -> str:
    proc = subprocess.run(
        ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short-iso"],
        capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else proc.stderr

def _launchd_plist_dirs():
    return [
        Path.home() / "Library" / "LaunchAgents",
        Path("/Library/LaunchAgents"),
        Path("/Library/LaunchDaemons"),
    ]

def _read_plist(path: Path):
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return None

def _plist_for_label(label: str):
    for d in _launchd_plist_dirs():
        candidate = d / f"{label}.plist"
        if candidate.exists():
            return candidate, _read_plist(candidate)
    for d in _launchd_plist_dirs():
        if not d.exists(): continue
        for p in d.glob("*.plist"):
            data = _read_plist(p)
            if data and data.get("Label") == label:
                return p, data
    return None, None

def _owns_plist(data: dict) -> bool:
    if not data: return False
    base      = str(BASE_DIR)
    prog_args = data.get("ProgramArguments") or []
    wd        = data.get("WorkingDirectory") or ""
    return base in " ".join(str(a) for a in prog_args) or base in str(wd)

def _list_related_services_mac():
    found = []
    for d in _launchd_plist_dirs():
        if not d.exists(): continue
        for p in d.glob("*.plist"):
            data = _read_plist(p)
            if data and _owns_plist(data):
                label = data.get("Label") or p.stem
                if SAFE_UNIT.match(label):
                    found.append(label)
    return sorted(set(found))

def _unit_exists_mac(label: str) -> bool:
    proc = subprocess.run(["launchctl", "list", label], capture_output=True, text=True)
    return proc.returncode == 0

def _service_log_mac(label: str, lines: int) -> str:
    _, data    = _plist_for_label(label)
    log_paths  = []
    if data:
        for key in ("StandardOutPath", "StandardErrorPath"):
            v = data.get(key)
            if v: log_paths.append(Path(v))
    for lp in log_paths:
        if lp.exists():
            try:
                with open(lp, "rb") as f:
                    tail_bytes = b"".join(deque(f, maxlen=lines))
                return tail_bytes.decode("utf-8", errors="replace")
            except Exception:
                continue
    proc = subprocess.run(
        ["log", "show", "--style", "syslog", "--last", "2h",
         "--predicate", f'process == "{label}"'],
        capture_output=True, text=True
    )
    out = proc.stdout if proc.returncode == 0 else proc.stderr
    if not out.strip():
        out = ("No log file configured for this service. Set StandardOutPath/StandardErrorPath "
               "in its plist to a file under this app's logs/ directory.")
    return out

def _list_related_services_win():
    found = []
    try:
        proc = subprocess.run(
            ["sc", "query", "type=", "all", "state=", "all"],
            capture_output=True, text=True
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("SERVICE_NAME:"):
                svc = line.split(":", 1)[1].strip()
                if not SAFE_UNIT.match(svc): continue
                qc = subprocess.run(["sc", "qc", svc], capture_output=True, text=True)
                if str(BASE_DIR) in qc.stdout:
                    found.append(svc)
    except Exception:
        pass
    return sorted(set(found))

def _service_log_win(service: str, lines: int) -> str:
    log_path = LOGS_DIR / f"{service}.log"
    if log_path.exists():
        try:
            with open(log_path, "rb") as f:
                tail_bytes = b"".join(deque(f, maxlen=lines))
            return tail_bytes.decode("utf-8", errors="replace")
        except Exception:
            pass
    return f"No log file found at {LOGS_DIR / service}.log"

def list_related_services():
    if IS_MAC:    return _list_related_services_mac()
    if IS_LINUX:  return _list_related_services_linux()
    if IS_WINDOWS: return _list_related_services_win()
    return []

def _service_log(unit: str, lines: int) -> str:
    if IS_MAC:    return _service_log_mac(unit, lines)
    if IS_LINUX:  return _service_log_linux(unit, lines)
    if IS_WINDOWS: return _service_log_win(unit, lines)
    return "Service log viewing not supported on this platform."

@app.get("/api/services")
def api_services():
    return jsonify(list_related_services())

@app.get("/api/service-log/<unit>")
def api_service_log(unit):
    if not SAFE_UNIT.match(unit):
        abort(400, "invalid unit name")
    try:
        lines = int(request.args.get("lines", 500))
    except ValueError:
        lines = 500
    text = _service_log(unit, lines)
    return Response(text or "", mimetype="text/plain", headers={"Cache-Control": "no-store"})

@app.route("/services")
def services():
    return render_template("services.html")

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host  = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port  = int(os.environ.get("DASHBOARD_PORT", "5001"))
    debug = os.environ.get("DASHBOARD_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
