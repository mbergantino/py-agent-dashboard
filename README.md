# Py Script Dashboard

A lightweight, local-only web UI for managing and monitoring Python scripts on a Raspberry Pi (or any Debian-like box). Add/edit scripts in your browser, attach cron schedules, view logs with auto-refresh, and run jobs on demand. Includes a resilient runner that auto-installs missing Python dependencies.

---

## Highlights

* **Script Manager**: Create, edit, delete Python scripts stored on disk.
* **Scheduling**: Attach a cron expression per script (or type `DISABLED` to turn it off).
* **One-click Sample**: Add a working `helloworld` job from the index page.
* **Run Now**: Trigger a script immediately from the UI.
* **Logs**: Per-job log files, in-browser viewer with **Refresh**, **Auto-refresh**, **Follow tail**, and line count.
* **Cron Sync**: Rebuilds the job list by reconciling `scripts/` with the user’s crontab.
* **Smart runner**: Wraps your scripts to attempt to auto-heal from `ModuleNotFoundError`, it:
  * Tries `pip install` (with Debian PEP-668 support: `--break-system-packages`)
  * Falls back to `apt-get install python3-<pkg>` for common libs (if running as root)
  * Retries **as long as** the last attempt installed a missing dependency

---

## Project Layout

```
pi_cron_dashboard/
├─ dashboard.py           # Flask app
├─ runner.py              # Smart script runner (auto-installs deps)
├─ scripts/               # Your .py jobs live here
├─ logs/                  # Per-job logs live here
└─ templates/
   ├─ index.html          # Jobs table (+ Add Sample)
   ├─ add.html            # Create new script by name
   ├─ edit.html           # Editor + cron schedule (supports "DISABLED")
   ├─ services.html       # Systemd log viewer
   └─ view.html           # Log viewer with auto-refresh
```

---

## Requirements

* Python 3.9+ (works on Raspberry Pi OS / DietPi)
* Flask
* python-crontab

Install via APT (Debian/DietPi/Raspberry Pi OS):

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-flask python3-crontab python3-pytz
```

Or via pip (inside a virtualenv recommended):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install Flask python-crontab
```

> The **runner** handles most user-script dependencies (e.g., `requests`, `bs4`) at runtime. See **Security notes** below.

---

## Quick Start

```bash
# clone your repo (example)
git clone git@github.com:<you>/pi_cron_dashboard.git
cd pi_cron_dashboard

# run the web app
python3 dashboard.py
# browse: http://<pi-ip>:5000
```

### Add the sample job

On the index page, click **“Add Sample”**.
It creates `scripts/helloworld.py`, which when run generates a single timestamped log entry.

---

## Runner (auto-install dependencies)

`runner.py` executes your script and handles missing modules:

* Tries `pip install <module>` (adds `--break-system-packages` if needed on Debian/DietPi)
* If that fails and running as root, tries `apt-get install python3-<module>`
* Retries execution **while** the last attempt installed a dependency (safety cap configurable)

Environment variables:

* `RUNNER_MAX_PASSES` (default: `50`) — upper bound on retries
* `ALLOW_AUTO_INSTALL` inside `runner.py` — set to `False` to disable auto-installs

Optional per-script header to pre-install:

```python
# requirements: requests beautifulsoup4 lxml
```

---

## Log viewer

* Open any job → **Logs**.
* Controls:

  * **Refresh**
  * **Auto-refresh** (enabled by default), interval picker
  * **Follow** (scroll sticks to the bottom like `tail -f`)
  * **Lines** (server tails last N lines)
* Backed by `/api/log/<name>?lines=500`.

---

## Security notes

* The runner may `pip install` as root (or with `--break-system-packages` on Debian). This is convenient but **powerful**:
  * Prefer running the dashboard under a **non-root user** or inside a **virtualenv**.
  * Consider setting `ALLOW_AUTO_INSTALL = False` and pinning dependencies via a requirements file if you need stricter control.
* Only expose the dashboard on trusted networks (it’s meant for **LAN-only** use).

---

## Contributing

PRs welcome! Ideas:

* Per-job environment variables
* Auth (HTTP basic / token)
* Systemd timers alternative to cron
* Export/import job definitions

