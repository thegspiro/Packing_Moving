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
| `app/static/qrcode.js` | QR code library ([qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator), MIT — see `app/static/THIRD_PARTY_LICENSES.md`) |
| `unraid/packing-tracker.xml`, `unraid/icon.png` | Unraid Docker template and icon |
| `.github/workflows/docker.yml` | Runs the tests and publishes the image to `ghcr.io/thegspiro/packing_moving` |
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
| `PUBLIC_URL` | *(empty)* | Address phones use to reach the tracker, e.g. `http://192.168.1.50:5639`. QR codes on labels point here. If empty, they use the address you print from. An invalid value stops the container from starting. |
| `PUID` / `PGID` | `99` / `100` | User and group the app runs as when the container is started as root (the Unraid template does this). The data folder is given to this user first. Ignored when started with `--user` or compose `user:`. |
| `DATA_PATH` | `/mnt/user/appdata/packing-tracker` | Compose only: host folder mounted at `/data`. |

## Install on Unraid (template)

The image is built and published by GitHub automatically, so there's nothing
to build on the server.

1. Download the template (in the Unraid terminal):

   ```bash
   curl -fsSL -o /boot/config/plugins/dockerMan/templates-user/my-packing-tracker.xml https://raw.githubusercontent.com/thegspiro/Packing_Moving/main/unraid/packing-tracker.xml
   ```

2. On the **Docker** tab, click **Add Container**, then pick **packing-tracker**
   from the **Template** list (under *User templates*).
3. Fill in **Password** (at least 8 characters) and **Public URL**
   (`http://<unraid-ip>:5639`, used by the QR codes). The port and data folder
   defaults are fine. Click **Apply**.
4. Click the tracker's icon on the Docker tab and choose **WebUI**, or open
   `http://<unraid-ip>:5639`, and sign in.

The container starts as root only long enough to give its data folder to
`nobody:users` (99:100), then runs as that user.

**Do not forward port 5639 on your router.** The tracker uses plain HTTP and is
meant for your home network only. For access away from home, use a VPN such as
WireGuard or Tailscale.

### One-time GitHub setup

The first time GitHub publishes the image, it may be private, and Unraid would
then fail to download it with "denied". To fix that once: on GitHub, open your
profile → **Packages** → **packing_moving** → **Package settings** → **Change
visibility** → **Public**.

## Install with Docker Compose (alternative)

Builds the image on the server from this repository instead of downloading it.

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

The container runs as `nobody:users` (99:100), not root. Don't run both the
template and the compose version at once: they use the same port and data folder.

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

**Template install:** every change merged into `main` is tested and published
by GitHub (see the **Actions** tab). On Unraid's **Docker** tab, click **Check
for Updates**, then **apply update** on the tracker. Your settings and box data
are kept.

**Compose install:** download the latest code and rebuild. Your `.env` and your
box data are not part of the repository, so updating never touches them.

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

## QR codes

4" x 6" and 3" x 2" labels include a QR code (2" x 1" labels are too small).
Scanning it with a phone opens that box in the tracker, showing its contents
with one-tap buttons to move it (for example to **Truck** or **New apartment**),
plus Edit and Print. Phones must be on your home network (or VPN) and signed in;
a phone stays signed in for 30 days.

Set **Public URL** in the template (or `PUBLIC_URL` in `.env` for compose) to
the address phones should use — normally `http://<unraid-ip>:5639` — then
restart the container. If it's empty, the codes
use whatever address you printed from, and the tracker warns you before
printing codes that point to `localhost`, which phones can't open.

Codes link to the box itself, not its number, so renumbering a box doesn't
break its label. If you change `PUBLIC_URL` (for example a new server IP),
reprint the labels. Labels printed before QR codes were added still work; they
just have no code.

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

The browser test needs Node.js and Playwright, and a running server with an empty database:

```bash
BASE_URL=http://127.0.0.1:5639 TRACKER_PASSWORD=your-password node tests/e2e_smoke.js
```
