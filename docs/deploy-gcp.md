# Deploying MercBot on Google Cloud (free tier)

MercBot runs on one always-free `e2-micro` VM, alongside the Cloudflare tunnel that already routes
`discord-dev.tostr.ai` to it. The VM needs no open ports: the tunnel connects out to Cloudflare, and
the bot only listens on `localhost:8787`.

Commands marked **(Mac)** run on your laptop; **(server)** run on the VM over SSH.

## 1. Google Cloud setup (once)

1. **(Mac)** Install and sign in to the Google Cloud CLI:

   ```sh
   brew install --cask google-cloud-sdk
   gcloud init        # sign in, then pick or create a project for MercBot
   gcloud services enable compute.googleapis.com
   gcloud config set compute/zone us-central1-a
   ```

   Project IDs are unique across all of Google Cloud, so a plain name like `mercbot` is usually
   taken; creating the project in the Cloud Console suggests a free ID (e.g. `mercbot-511703`).
   The project needs a billing account linked before Compute Engine can be enabled
   (**Billing → Link a billing account**). `gcloud init` printing a Compute Engine
   `SERVICE_DISABLED` error is expected before the `services enable` step and can be ignored.

2. In the Cloud Console, go to **Billing → Budgets & alerts** and create a budget of **$1** with
   email alerts, so anything that does start costing money is caught early.

3. **(Mac)** Create the VM. All of these settings matter for staying free: `e2-micro`, one of the
   `us-west1` / `us-central1` / `us-east1` regions, and a **standard** (`pd-standard`) disk of at
   most 30 GB (the default disk type is billed).

   ```sh
   gcloud compute instances create mercbot \
     --zone=us-central1-a \
     --machine-type=e2-micro \
     --image-family=debian-12 --image-project=debian-cloud \
     --boot-disk-size=30GB --boot-disk-type=pd-standard
   ```

## 2. Install the bot

1. **(Mac)** Copy the secrets and the tunnel credentials to the VM:

   ```sh
   gcloud compute scp .env ~/.cloudflared/<TUNNEL-ID>.json mercbot:/tmp/ --zone=us-central1-a
   gcloud compute ssh mercbot --zone=us-central1-a
   ```

   `<TUNNEL-ID>` is the `.json` file's name in `~/.cloudflared/`.

2. **(server)** Install git and [uv](https://docs.astral.sh/uv/), create a user for the bot, and
   install it (uv fetches the same Python version used in development):

   ```sh
   sudo apt-get update && sudo apt-get install -y git
   curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
   sudo useradd --system --create-home --home-dir /var/lib/mercbot --shell /usr/sbin/nologin mercbot

   sudo git clone https://github.com/rubyTanuki/MercBot.git /opt/mercbot
   sudo chown -R mercbot:mercbot /opt/mercbot
   cd /opt/mercbot
   sudo -u mercbot uv venv --python 3.14 .venv
   sudo -u mercbot uv pip install --python .venv/bin/python -r requirements.txt

   sudo install -o mercbot -g mercbot -m 600 /tmp/.env /opt/mercbot/.env
   sudo install -m 644 deploy/mercbot.service /etc/systemd/system/mercbot.service
   sudo systemctl daemon-reload
   ```

   If the GitHub repo is private, clone it with a
   [deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
   instead.

3. **(server)** Install `cloudflared` and point it at the tunnel:

   ```sh
   curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
   echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
     | sudo tee /etc/apt/sources.list.d/cloudflared.list
   sudo apt-get update && sudo apt-get install -y cloudflared

   sudo mkdir -p /etc/cloudflared
   sudo install -m 600 /tmp/<TUNNEL-ID>.json /etc/cloudflared/
   tunnel_id=$(sudo sh -c 'basename /etc/cloudflared/*.json .json')
   sudo sed "s/<TUNNEL-ID>/$tunnel_id/" /opt/mercbot/deploy/cloudflared-config.yml | sudo tee /etc/cloudflared/config.yml
   rm /tmp/.env /tmp/<TUNNEL-ID>.json
   ```

Paste these one line at a time: a pasted block keeps going after a failed line, so errors are easy to miss.

## 3. Switch over from the laptop

Stop the laptop's copies **first**. If both machines run `cloudflared`, Cloudflare splits requests
between them, and the bot on whichever one isn't running would make Discord commands fail.

1. **(Mac)** Stop the bot (Ctrl+C in its terminal) and `cloudflared` (Ctrl+C, or
   `sudo cloudflared service uninstall` if it was installed as a service).
2. **(server)** Start both, and have them start on every boot:

   ```sh
   sudo systemctl enable --now mercbot
   sudo cloudflared service install
   sudo systemctl enable --now cloudflared
   ```

3. **(server)** Check the tunnel is connected: `sudo journalctl -u cloudflared -n 20 --no-pager`
   should show `Registered tunnel connection`. If the public URL returns HTTP 530, no machine is
   connected to the tunnel.
4. **(server)** Check the bot started and registered its commands:

   ```sh
   journalctl -u mercbot -n 30 --no-pager
   ```

   You should see `registered commands in 2 server(s)` and `Uvicorn running on http://127.0.0.1:8787`.
5. Run `/poll` in Discord to confirm it answers.
6. After a day, check **Billing → Reports** shows $0.00. If anything shows a charge (the VM's
   public IP is the most likely candidate), it'll be listed there.

## Day to day

| Task | Command (server) |
| --- | --- |
| Deploy new code (after pushing to GitHub) | `sh /opt/mercbot/deploy/update.sh` |
| Watch the bot's logs | `journalctl -u mercbot -f` |
| Restart the bot | `sudo systemctl restart mercbot` |
| Edit secrets | `sudoedit /opt/mercbot/.env`, then restart |
| Get a Linear approval link | `cd /opt/mercbot/src && sudo -u mercbot ../.venv/bin/python -m MercBot.main linear-link` |

The Linear link can also still be generated on the laptop. Both read the same Cloudflare KV, so
either machine's link completes on the server.

SSH in with `gcloud compute ssh mercbot --zone=us-central1-a`.
