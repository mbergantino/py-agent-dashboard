# Py Agent Dashboard

A lightweight, local-only web UI for managing and monitoring scheduled jobs on any platform (Linux, macOS, Windows). Create and edit jobs in your browser, attach cron schedules, view logs with auto-refresh, and trigger jobs on demand. Each job lives in its own folder and can contain any number of files.

---

## Highlights

* **Job Manager**: Create, edit, rename, and delete jobs. Each job is a folder under `jobs/` that can hold any mix of files (`.py`, `.sh`, `.md`, CSV, XLS/XLSX, etc.).
* **Multi-file editor**: Per-job file list with rename, clone, delete, drag-to-reorder (desktop) and tap-to-reorder (mobile). File content edits with live Python syntax validation.
* **Execution command**: Each job can specify a custom execution command and parameters (e.g. `python3 -u main.py`, `bash run.sh`, `node index.js`). Defaults to `runner.py` with auto-dependency healing for Python jobs.
* **Scheduling**: Attach a cron expression per job (or `DISABLED`). Convenience buttons: Every min, Hourly, Daily, Weekly, Disable. Schedule previews show last and next execution times in your local timezone.
* **Run Now**: Trigger any job immediately from the index or edit screen.
* **Logs**: Per-job log files with an in-browser viewer — Refresh, Auto-refresh, Follow tail, line count, Raw/Rich (ANSI color) toggle, and Purge.
* **Service Logs**: View logs for related systemd (Linux), launchd (macOS), or Windows services from within the dashboard.
* **Running indicator**: Live chip on the index page shows currently executing jobs.
* **Dark mode**: Light / Dark / Auto (follows system) toggle on every page. Single-tap on mobile.
* **Mobile-responsive**: Full mobile layout on all pages — collapsible file pane, touch reorder, compact header.
* **AI token storage**: Jobs with a `.md` file get a 🔑 button to store an encrypted AI auth token (Fernet symmetric encryption, key at `config/.key`).
* **Smart runner**: Wraps Python jobs to auto-heal `ModuleNotFoundError`:
  * Tries `pip install` (with Debian PEP-668 support: `--break-system-packages`)
  * Falls back to `apt-get install python3-<pkg>` if running as root
  * Retries while the last attempt installed a missing dependency
* **Cross-platform scheduling**: cron (Linux/macOS) or Windows Task Scheduler — same UI.
* **One-click sample**: Adds a working `helloworld` job from the index page.

---

## Project Layout

```
pi_cron_dashboard/
├─ dashboard.py           # Flask app
├─ runner.py              # Smart job runner (auto-installs Python deps)
├─ jobs/                  # One subfolder per job
│   └─ <slug>/            #   job files live here (any type)
├─ config/                # Per-job JSON configs + encryption key
├─ logs/                  # Per-job log files (<slug>.log)
└─ templates/
   ├─ index.html          # Jobs table
   ├─ edit.html           # Editor + file manager + cron schedule
   ├─ view.html           # Log viewer
   └─ services.html       # System service log viewer
```

---

## Requirements

Python 3.9+, Flask, python-crontab, pytz.

```bash
# Debian / Raspberry Pi OS / DietPi
sudo apt-get install -y python3 python3-pip python3-flask python3-crontab python3-pytz

# Or via pip (virtualenv recommended)
pip install Flask python-crontab pytz
```

**Optional extras:**

| Package | Feature enabled |
|---|---|
| `openpyxl` | In-browser XLS/XLSX table viewer |
| `cryptography` | Encrypted AI auth token storage |

```bash
pip install openpyxl cryptography
```

---

## Quick Start

```bash
git clone https://github.com/<you>/pi_cron_dashboard.git
cd pi_cron_dashboard
python3 dashboard.py
# browse: http://localhost:5001
```

Environment variables (all optional):

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_HOST` | `0.0.0.0` | Bind address |
| `DASHBOARD_PORT` | `5001` | Port |
| `DASHBOARD_DEBUG` | `0` | Flask debug mode (`1` to enable) |

---

## Jobs

Each job is a directory under `jobs/<slug>/`. The **active file** (first `.py` or first file in the configured order) is what `runner.py` executes by default. A custom **Execution Command** in the job config overrides this entirely — use it to run shell scripts, Node.js, or any other command.

### Adding a job

1. Click **+ Add Job** on the index page.
2. Enter a display name. A slug is derived automatically.
3. Set a cron schedule or leave as `DISABLED`.
4. After creation, use the editor to upload or create files.

### File types

| Extension | Editor behavior |
|---|---|
| `.py` | Code editor with live syntax validation |
| `.csv` | Table view or raw text toggle |
| `.xlsx` / `.xls` | Table view (requires `openpyxl`) |
| `.md` | Text editor; enables AI token button |
| Anything else | Plain text editor |

---

## Runner (auto-install dependencies)

`runner.py` executes the active file and handles missing Python modules:

* Tries `pip install <module>` (adds `--break-system-packages` on Debian/DietPi)
* If that fails and running as root, tries `apt-get install python3-<module>`
* Retries while the last attempt installed at least one dependency
* Cap: `RUNNER_MAX_PASSES` env var (default `50`)

Pre-declare dependencies in a comment at the top of your script:

```python
# requirements: requests beautifulsoup4 lxml
```

Set `ALLOW_AUTO_INSTALL = False` inside `runner.py` to disable auto-installs.

---

## Config

Each job's config is stored at `config/<slug>.json`:

```json
{
  "display_name": "My Job",
  "schedule": "0 9 * * 1",
  "exec_command": "",
  "active_file": "main.py",
  "file_order": ["main.py", "helpers.py", "notes.md"],
  "run_until_success": false,
  "auth_token_enc": "<fernet-encrypted>",
  "auth_token_env": "MY_API_KEY"
}
```

`config/_order.json` stores the index-page row order (drag-to-reorder).

---

## Environment Variables

Credentials and per-machine settings live in a `.env` file at the project root. Copy the template and fill in your values:

```bash
cp .env.example .env
```

| Variable | Used by | Description |
|---|---|---|
| `NOTIFY_SMTP_HOST` | all jobs | SMTP server (e.g. `smtp.gmail.com`) |
| `NOTIFY_SMTP_PORT` | all jobs | SMTP port (default `587`) |
| `NOTIFY_SMTP_USER` | all jobs | SMTP login / From address |
| `NOTIFY_SMTP_PASSWORD` | all jobs | App password — see [Google App Passwords](https://myaccount.google.com/apppasswords) |
| `NOTIFY_FROM_ADDR` | all jobs | From header (defaults to `NOTIFY_SMTP_USER`) |
| `NOTIFY_DEFAULT_TO` | all jobs | Default recipient |
| `RCCL_USERNAME` | watch_for_royal_price_changes_xls | Royal Caribbean account email |
| `RCCL_PASSWORD` | watch_for_royal_price_changes_xls | Royal Caribbean account password |
| `RCCL_VDS_ID` | watch_for_royal_price_changes_xls | Royal Caribbean VDS ID (UUID) |

`runner.py` loads `.env` automatically at startup using a built-in parser — no `python-dotenv` dependency needed. Values already set in the shell or cron environment take precedence over the file.

All jobs send email through the shared `email_notifier.py` module (`send_alert()`), so updating credentials in one place covers every job.

---

## Security Notes

* **Local only** — no authentication. Bind to `127.0.0.1` or firewall port 5001 if on a shared network.
* AI tokens are encrypted at rest with Fernet (symmetric AES-128-CBC). The key lives at `config/.key` — keep it out of version control (`.gitignore` it).
* `runner.py` runs `pip install` and optionally `apt-get` as the dashboard user. Only use on machines you control.
