# Dinner Tab on do-personal

Runs like the other side projects there (menuvi, mealops): a systemd service
behind nginx, HTTPS from certbot.

| Thing | Where |
|---|---|
| Code | `/var/www/dinner` (git checkout of `dezgo/dinner`, public — no deploy key needed) |
| Secrets | `/var/www/dinner/.env` (untracked, mode 600) |
| Data | `/var/lib/dinner/` — `dinner.db` and `uploads/`, owned by www-data |
| Service | `dinner.service` → uvicorn on 127.0.0.1:8030, **one worker** |
| Site | `/etc/nginx/sites-available/dinner` (copied into `sites-enabled`) |
| Logs | `sudo journalctl -u dinner -f` |

## Deploys

Push to `main` → GitHub Actions runs lint and tests, then connects to
do-personal with the `github-actions-dinner-deploy` key. That key is locked in
`~/.ssh/authorized_keys` to run only `deploy/deploy.sh` (pull, install, restart,
wait for health), so it can't be used for anything else.

Repository secrets (Settings → Secrets and variables → Actions):

- `SSH_HOST` = `209.38.91.37`
- `SSH_KEY` = the private half of the dinner deploy key

## .env

```ini
ENV=production
SECRET_KEY=<long random>          # don't change: signs everyone out, breaks the Up webhook secret
ADMIN_PASSWORD=<long random>
TRUSTED_HOSTS=dinner.appfoundry.cc
TRUST_PROXY_HEADERS=true
PUBLIC_BASE_URL=https://dinner.appfoundry.cc
ANTHROPIC_API_KEY=                # optional: photo reading
UP_API_TOKEN=                     # optional: automatic payment detection
```

After editing: `sudo systemctl restart dinner`.

`ENV=production` makes cookies HTTPS-only, so sign-in only works once the
certificate is in place.

## One-off steps needing your sudo password

```bash
sudo certbot --nginx -d dinner.appfoundry.cc
```

## Don't

- Delete `/var/lib/dinner` (all dinners, payments, photos, the encrypted webhook secret).
- Run more than one worker.
