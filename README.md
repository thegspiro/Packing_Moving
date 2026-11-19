# Packing Tracker

Track what's in each moving box, where each box is, and print labels on a Rollo
(or any) label printer at 4" x 6", 3" x 2" or 2" x 1".

The data is stored on the server in a single SQLite file, so every browser and
device sees the same boxes. Sign-in uses one shared password.

## What's in this folder

| Path | Purpose |
|---|---|
| `app/server.py` | Web server and JSON API (Python 3 standard library only) |
| `app/static/index.html` | The web page |
| `Dockerfile`, `docker-compose.yml` | Container setup |
| `.env.example` | Settings template — copy to `.env` (never commit `.env`) |
| `tests/test_server.py` | Server tests |
| `tests/e2e_smoke.js` | Browser test (optional, needs Playwright) |

## Settings

| Variable | Default | Notes |
|---|---|---|
| `TRACKER_PASSWORD` | *(none)* | **Required**, at least 8 characters. The container will not start without it. Changing it signs everyone out. |
| `PORT` | `5639` | Port inside the container. |
| `DATA_DIR` | `/data` | Where `tracker.db` lives inside the container. |
| `BIND_ADDRESS` | `0.0.0.0` | Address to listen on. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `DATA_PATH` | `/mnt/user/appdata/packing-tracker` | Compose only: host folder mounted at `/data`. |

## Install on Unraid

1. Download the code to the server (in the Unraid terminal):

   ```bash
   git clone https://github.com/thegspiro/Packing_Moving.git /mnt/user/appdata/packing-tracker-app
   ```

   If the Unraid terminal says `git: command not found`, run the same thing
   through Docker instead:

   ```bash
   docker run --rm -v /mnt/user/appdata:/git alpine/git clone https://github.com/thegspiro/Packing_Moving.git packing-tracker-app
   ```

2. Create the data folder (in the Unraid terminal):

   ```bash
   mkdir -p /mnt/user/appdata/packing-tracker && chown 99:100 /mnt/user/appdata/packing-tracker
   ```

3. Create your settings file and set a password:

   ```bash
   cd /mnt/user/appdata/packing-tracker-app && cp .env.example .env && nano .env
   ```

   Easiest is a password of letters, numbers, `-` and `_`. Otherwise: with
   Docker Compose, wrap it in single quotes; with plain `docker run`, never use
   quotes (they become part of the password).

4. Build and start it, using **one** of these:

   **With the Docker Compose Manager plugin** (from Community Applications):

   ```bash
   cd /mnt/user/appdata/packing-tracker-app && docker compose up -d --build
   ```

   **With plain Docker** (no plugin needed):

   ```bash
   cd /mnt/user/appdata/packing-tracker-app && docker build -t packing-tracker:latest .
   ```

   ```bash
   docker run -d --name packing-tracker --restart unless-stopped --user 99:100 -p 5639:5639 --env-file /mnt/user/appdata/packing-tracker-app/.env -v /mnt/user/appdata/packing-tracker:/data packing-tracker:latest
   ```

5. Open `http://<unraid-ip>:5639` and sign in.

The container shows up on Unraid's Docker tab, where you can stop, start and
view its log. It runs as `nobody:users` (99:100), not root.

**Do not forward port 5639 on your router.** The tracker uses plain HTTP and is
meant for your home network only. For access away from home, use a VPN such as
WireGuard or Tailscale.

## Moving boxes from the old single-file version

The old version saved boxes inside the browser. To bring them over:

1. Open the old HTML file, click **Export backup**, and save the `.json` file.
2. Open the new tracker, sign in, click **Import backup**, and pick that file.

If the new tracker is opened in the same browser *at the same address* where the
old data was saved, it offers to copy the boxes automatically. An HTML file
opened from your desktop counts as a different address, so most people will
need the export/import steps above.

Importing **replaces** all boxes on the server.

## Backups

- Easiest: click **Export backup** in the tracker now and then.
- Or back up `/mnt/user/appdata/packing-tracker` while the container is stopped
  (the Appdata Backup plugin does this for you). Copying `tracker.db` while the
  container is running can miss recent changes.

## Updating

Download the latest code and rebuild. Your `.env` and your box data are not
part of the repository, so updating never touches them.

```bash
cd /mnt/user/appdata/packing-tracker-app && git pull && docker compose up -d --build
```

Without `git` on the server, pull through Docker instead, then rebuild:

```bash
docker run --rm -v /mnt/user/appdata/packing-tracker-app:/git alpine/git pull
```

```bash
cd /mnt/user/appdata/packing-tracker-app && docker compose up -d --build
```

If you started the container with plain `docker run`, rebuild with
`docker build -t packing-tracker:latest .`, then `docker rm -f packing-tracker`
and run the same `docker run` command as before.

Database changes are applied automatically on start. If you go back to an older
version after the database was upgraded, the container refuses to start rather
than risk your data; restore a backup or reinstall the newer version.

## Printing labels

Each Print button asks for the label size and number of copies (1–4). In the
browser's print window, choose the paper size that matches your roll, set
Margins to **None** and Scale to **Default/100%**. The printer must offer that
paper size — for a wireless Rollo, add custom sizes in the Rollo app.

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

The browser test needs Node.js and Playwright, and a running server with an empty database:

```bash
BASE_URL=http://127.0.0.1:5639 TRACKER_PASSWORD=your-password node tests/e2e_smoke.js
```
