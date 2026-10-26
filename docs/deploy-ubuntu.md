# Deploying MercBot on an Ubuntu server

MercBot runs on any Ubuntu server (22.04 or 24.04) behind its own Cloudflare tunnel. The server
needs no open ports besides SSH: the tunnel connects out to Cloudflare, and the bot only listens on
`localhost:8787`.

Throughout, replace:

- `<SERVER>` with how you SSH in, e.g. `ubuntu@203.0.113.7` (a user with `sudo`).
- `<HOSTNAME>` with the public hostname the bot will answer on, e.g. `mercbot.example.com`. Its
  domain must be a zone in your Cloudflare account (added under **Add a site**, with the domain's
  nameservers pointed at Cloudflare and the zone showing **Active**).

Commands marked **(Mac)** run on your laptop; **(server)** run on the server over SSH. Paste them
one line at a time: a pasted block keeps going after a failed line, so errors are easy to miss.

## 1. Prepare the server

**(server)** Update it and turn on the firewall, allowing only SSH in:

```sh
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y git curl
sudo ufw allow OpenSSH
sudo ufw enable
```

## 2. Install the bot

1. **(Mac)** Copy the secrets to the server:

   ```sh
   scp .env <SERVER>:/tmp/mercbot.env
   ```

2. **(server)** Install [uv](https://docs.astral.sh/uv/), create a user for the bot, and install it
   (uv fetches the same Python version used in development). Everything the bot uses lives in
   `/opt/mercbot`: it's the checkout and also the bot user's home, so uv's Python and cache land in
   its `.local/` and `.cache/` (both gitignored). Only the systemd and `cloudflared` config go in
   `/etc`.

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh

   sudo git clone https://github.com/rubyTanuki/MercBot.git /opt/mercbot
   sudo useradd --system --no-create-home --home-dir /opt/mercbot --shell /usr/sbin/nologin mercbot
   sudo chown -R mercbot:mercbot /opt/mercbot
   cd /opt/mercbot
   sudo -u mercbot uv venv --python 3.14 .venv
   sudo -u mercbot uv pip install --python .venv/bin/python -r requirements.txt

   sudo install -o mercbot -g mercbot -m 600 /tmp/mercbot.env /opt/mercbot/.env
   rm /tmp/mercbot.env
   sudo install -m 644 deploy/mercbot.service /etc/systemd/system/mercbot.service
   sudo systemctl daemon-reload
   ```

3. **(server)** Point the bot at its new hostname. Open the secrets with `sudoedit /opt/mercbot/.env`
   and set (add it, or replace an existing line):

   ```
   PUBLIC_BASE_URL=https://<HOSTNAME>
   ```

   Without it the bot builds Linear links for `discord-dev.tostr.ai`.

4. **(server)** Start the bot and check it registered its commands:

   ```sh
   sudo systemctl enable --now mercbot
   journalctl -u mercbot -n 30 --no-pager
   ```

   You should see `registered commands in 2 server(s)` and `Uvicorn running on http://127.0.0.1:8787`.
   Discord still sends commands to the old deployment until step 4.

## 3. Create the tunnel

This makes a new tunnel, separate from `discord-dev`, so the old deployment keeps working until you
switch over.

1. **(server)** Install `cloudflared`:

   ```sh
   sudo curl -fsSL -o /usr/share/keyrings/cloudflare-main.gpg https://pkg.cloudflare.com/cloudflare-main.gpg
   echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
     | sudo tee /etc/apt/sources.list.d/cloudflared.list
   sudo apt-get update && sudo apt-get install -y cloudflared
   ```

2. **(server)** Log in to Cloudflare. This prints a link: open it on your laptop, and pick
   **`<HOSTNAME>`'s domain** (not `tostr.ai`), since DNS records are created in the zone picked here.

   ```sh
   cloudflared tunnel login
   ```

3. **(server)** Create the tunnel and route the hostname to it (this adds a `CNAME` for
   `<HOSTNAME>` in Cloudflare DNS; delete any existing record for that name first):

   ```sh
   host=<HOSTNAME>
   cloudflared tunnel create mercbot
   cloudflared tunnel route dns mercbot "$host"
   ```

4. **(server)** Install the tunnel's credentials and config, and run it as a service:

   ```sh
   sudo mkdir -p /etc/cloudflared
   sudo install -m 600 ~/.cloudflared/*.json /etc/cloudflared/
   tunnel_id=$(sudo sh -c 'basename /etc/cloudflared/*.json .json')
   sudo sed -e "s/<TUNNEL-ID>/$tunnel_id/" -e "s/<HOSTNAME>/$host/" \
     /opt/mercbot/deploy/cloudflared-config.yml | sudo tee /etc/cloudflared/config.yml
   sudo cloudflared service install
   sudo systemctl enable --now cloudflared
   ```

   `$host` is the one set in step 3; set it again if you've logged out since.

5. **(server)** Check it: `sudo journalctl -u cloudflared -n 20 --no-pager` should show
   `Registered tunnel connection`. From the laptop, `curl -i https://<HOSTNAME>/mercbot` should get
   an answer from the bot (HTTP 405, since it only accepts `POST`). HTTP 530 means the tunnel isn't
   connected; 404 means the hostname in `/etc/cloudflared/config.yml` doesn't match.

`~/.cloudflared/cert.pem` on the server is only needed for managing tunnels (creating, routing,
deleting); the running tunnel uses `/etc/cloudflared/` alone.

## 4. Switch over

A Discord application has one Interactions Endpoint URL, so changing it moves every command to the
new server at once.

1. In the [Discord Developer Portal](https://discord.com/developers/applications), open the app's
   **General Information** and set **Interactions Endpoint URL** to `https://<HOSTNAME>/mercbot`,
   then **Save Changes**. Discord checks the URL before saving, so this only succeeds while the new
   bot and tunnel are up.
2. Run `/poll` in Discord and confirm it answers; `journalctl -u mercbot -f` on the server shows it
   arriving.
3. Stop the old deployment (the laptop, or `sudo systemctl disable --now mercbot` on the old VM). It
   no longer gets commands, but would still share Linear's tokens in KV with the new one.
4. In Linear (**Settings → API → OAuth applications → MercBot**), add
   `https://<HOSTNAME>/mercbot/linear/callback` to the callback URLs. Linear stays connected: the
   tokens live in Cloudflare KV, which the new server reads too. The callback only matters the next
   time someone runs `linear-link`.
5. **(Mac)** Set `PUBLIC_BASE_URL=https://<HOSTNAME>` in the laptop's `.env` as well, so links made
   with `linear-link` there complete on the server.

The old `discord-dev` tunnel and its DNS record can be removed once nothing uses them
(`cloudflared tunnel delete discord-dev` on the Mac, and the record in the `tostr.ai` zone).

## Automatic deploys

The server checks GitHub every minute and deploys new commits on `main` by itself: it pulls,
installs any changed requirements and restarts the bot. It only fast-forwards, so if the server's
checkout has local edits it logs an error and leaves the running bot alone.

**(server)** Turn it on once:

```sh
sudo install -m 644 /opt/mercbot/deploy/mercbot-autodeploy.service /opt/mercbot/deploy/mercbot-autodeploy.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mercbot-autodeploy.timer
```

Check it with `systemctl list-timers mercbot-autodeploy.timer` (next run within a minute) and
`journalctl -u mercbot-autodeploy -n 20 --no-pager` (a `deploying …` line after each push).

Changes to the `.service`/`.timer` files themselves need the install commands above re-run. New
settings still need adding to the server's `.env` by hand (before pushing the code that uses them).

## Day to day

| Task | Command (server) |
| --- | --- |
| Deploy new code | Push to `main`; it's live within a minute. To deploy right away: `sh /opt/mercbot/deploy/update.sh` |
| See recent deploys | `journalctl -u mercbot-autodeploy -n 20 --no-pager` |
| Watch the bot's logs | `journalctl -u mercbot -f` |
| Restart the bot | `sudo systemctl restart mercbot` |
| Edit secrets | `sudoedit /opt/mercbot/.env`, then restart |
| Get a Linear approval link | `cd /opt/mercbot/src && sudo -u mercbot ../.venv/bin/python -m MercBot.main linear-link` |

SSH in with `ssh <SERVER>`.
