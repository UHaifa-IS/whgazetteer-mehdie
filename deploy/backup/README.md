# MEHDIE database backup

Nightly off-server backup of the tools.mehdie.org database and uploaded files,
from **cs-mehdie-web01** to **a256-gc1-01**. The network only allows SSH in
that direction, so web01 sends the files. web01's key is locked to one script
on a256 that accepts tonight's upload and nothing else: web01 cannot list, read,
overwrite or delete stored backups.

| What | How | File |
| --- | --- | --- |
| Postgres database `mehdie` | `pg_dump --format=custom` | `mehdie.dump` |
| Postgres roles (needed for a full restore) | `pg_dumpall --globals-only` | `globals.sql` |
| Django media folder (uploads) | `tar` of `app/media` | `media.tar.gz` |

Kept on a256 in `/srv/backup/mehdie/`: 14 daily, 8 weekly (Sunday), 12 monthly
(1st) copies. Weekly/monthly copies are hard links, so they cost no extra space
until the daily copy is pruned. On every run a256 checks the files (dump header,
table list, gzip, globals header), refuses a dump under 50% of the previous
night's size, writes `SHA256SUMS`, and stores nothing if anything failed.
Nothing is written to web01's disk; the dumps are streamed.

```
cs-mehdie-web01                                    a256-gc1-01
mehdie-backup.timer (02:17 nightly, root)
  └─ mehdie-backup-push ── ssh (key) ──▶  mehdie-backup-receive  (forced command,
       begin                                user mehdie-backup)
       pg_dump          │ put db      ──▶   .incoming-<date>/mehdie.dump
       pg_dumpall -g    │ put globals ──▶   .incoming-<date>/globals.sql
       tar media        │ put media   ──▶   .incoming-<date>/media.tar.gz
       commit                         ──▶   check → daily/<date>/, rotate
```

| File | Goes to |
| --- | --- |
| `web01/mehdie-backup-push` | web01: `/usr/local/sbin/mehdie-backup-push` |
| `web01/mehdie-backup.service`, `.timer` | web01: `/etc/systemd/system/` |
| `a256/mehdie-backup-receive` | a256: `/usr/local/bin/mehdie-backup-receive` |

## Setup

Run the commands from this folder (`deploy/backup` in a checkout of the repo)
on each server, e.g. after `git pull` or `scp -r deploy/backup <server>:`.

### 0. Checks

On **web01**:

```bash
nc -vz -w5 a256-gc1-01.srv.aau.dk 22             # must say "succeeded"
hostname -I                                       # web01's IP, for step 2
pg_dump --version                                 # note the major version
sudo -u postgres psql -Atc "SELECT pg_size_pretty(pg_database_size('mehdie'))"
ls -ld /srv/www/tools.mehdie.org/app/media        # if missing: INCLUDE_MEDIA=0 (step 1)
```

On **a256**, check there is room (several times the database size):

```bash
df -h /srv
```

### 1. web01: key, script, timer

```bash
sudo ssh-keygen -t ed25519 -N '' -C 'mehdie-backup@cs-mehdie-web01' -f /root/.ssh/id_ed25519_mehdie_backup
sudo cat /root/.ssh/id_ed25519_mehdie_backup.pub     # copy this line for step 2

sudo install -m 0755 web01/mehdie-backup-push /usr/local/sbin/mehdie-backup-push
sudo install -m 0644 web01/mehdie-backup.service web01/mehdie-backup.timer /etc/systemd/system/
# Only if there is no media folder:
#   echo 'INCLUDE_MEDIA=0' | sudo tee /etc/default/mehdie-backup
sudo systemctl daemon-reload
```

### 2. a256: user, receiver, key

```bash
sudo useradd --system --create-home --home-dir /var/lib/mehdie-backup --shell /bin/bash mehdie-backup
sudo passwd -l mehdie-backup                                    # no password login
sudo install -m 0755 a256/mehdie-backup-receive /usr/local/bin/mehdie-backup-receive
sudo install -d -m 0700 -o mehdie-backup -g mehdie-backup /srv/backup/mehdie
sudo apt install postgresql-client     # pg_restore >= web01's pg_dump major version (checks the dump)

sudo -u mehdie-backup mkdir -m 700 /var/lib/mehdie-backup/.ssh
# One line: only this key, only from web01, only the receiver, no shell/forwarding.
echo 'restrict,from="<WEB01-IP>",command="/usr/local/bin/mehdie-backup-receive" <PASTE THE .pub LINE FROM STEP 1>' \
  | sudo -u mehdie-backup tee /var/lib/mehdie-backup/.ssh/authorized_keys
sudo chmod 600 /var/lib/mehdie-backup/.ssh/authorized_keys
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub           # a256's fingerprint, for step 3
```

The shell must stay `/bin/bash` (sshd runs the forced command through it); the
`command=` restriction means the key can still only run the receiver. If sshd
has `AllowUsers`/`AllowGroups` set (`sudo sshd -T | grep -i allow`), add
`mehdie-backup` there and `sudo systemctl reload ssh`.

### 3. web01: trust a256, first run, enable

```bash
# Pin a256's host key; the fingerprint printed must match the one from step 2.
ssh-keyscan -t ed25519 a256-gc1-01.srv.aau.dk | sudo tee -a /root/.ssh/known_hosts
sudo ssh-keygen -lF a256-gc1-01.srv.aau.dk

sudo systemctl start mehdie-backup.service                     # first run, by hand
journalctl -u mehdie-backup -n 20 --no-pager                   # expect "committing" and "done"
sudo systemctl enable --now mehdie-backup.timer
systemctl list-timers mehdie-backup.timer
```

On a256: `sudo ls -la /srv/backup/mehdie/daily/*/` should show the four files.

## Checking it

On **web01** (where the job runs):
- `systemctl status mehdie-backup` - result of the last run (failed runs show as failed).
- `journalctl -u mehdie-backup` - what each run did.

On **a256** (where the files are):
- `journalctl -t mehdie-backup` - what was received, checked, stored, pruned, and why something was rejected.
- `sudo cat /srv/backup/mehdie/LAST_SUCCESS` - time of the last good backup.
- Verify files: `cd /srv/backup/mehdie/daily/<date> && sudo sha256sum -c SHA256SUMS`

There is no alerting yet. Look at `LAST_SUCCESS` now and then, or add
`OnFailure=` mail to the web01 service once the server can send mail.

If a256 rejects a dump as too small (and the database really did shrink), set
`MIN_SIZE_PCT=0` in `/etc/default/mehdie-backup` **on a256** for one run, then remove it.

## Restore test (do this once after setup, then e.g. every few months)

A backup is only proven by a restore. Restore into a scratch database on web01
and compare with the live one. web01 can't read from a256, so copy the dump
from your laptop (which can reach both):

```bash
# on your laptop
scp 'ot48zk@cs.aau.dk@a256-gc1-01.srv.aau.dk:/srv/backup/mehdie/daily/<date>/mehdie.dump' .
scp mehdie.dump 'ot48zk@cs.aau.dk@cs-mehdie-web01.srv.aau.dk:/tmp/'
```

(Files under `/srv/backup/mehdie` are only readable by `mehdie-backup`; on a256
first run `sudo cp /srv/backup/mehdie/daily/<date>/mehdie.dump /tmp/ && sudo chown $USER /tmp/mehdie.dump`
and copy from `/tmp/`, then delete it there.)

```bash
# on web01
sudo -u postgres createdb mehdie_restore_test
sudo -u postgres pg_restore --no-owner --exit-on-error -d mehdie_restore_test /tmp/mehdie.dump
for db in mehdie mehdie_restore_test; do
  sudo -u postgres psql -d "$db" -Atc "SELECT '$db', (SELECT count(*) FROM places), (SELECT count(*) FROM place_name), (SELECT count(*) FROM datasets)"
done
sudo -u postgres dropdb mehdie_restore_test && rm /tmp/mehdie.dump
```

The counts should match (allowing for changes since the dump). Delete the copy
on your laptop afterwards.

## Full restore (disaster)

On a rebuilt server with Postgres + PostGIS installed:

```bash
sudo -u postgres psql -f globals.sql                       # roles (errors for existing roles are fine)
sudo -u postgres createdb -O mehdie mehdie
sudo -u postgres pg_restore --exit-on-error -d mehdie mehdie.dump
sudo tar -C /srv/www/tools.mehdie.org/app -xzf media.tar.gz && sudo chown -R www-data: /srv/www/tools.mehdie.org/app/media
```

The knowledge graph does not need its own off-server backup: the CI pipeline
rebuilds it from this database (push to main or "Run pipeline" in GitLab).

## Security

- The backups contain user accounts (e-mail addresses, password hashes) and
  non-public datasets. On a256 they are readable only by `mehdie-backup` and root.
- web01's key can only run `mehdie-backup-receive`, only from web01's IP. That
  script takes `begin`, `put db|globals|media` and `commit` and nothing else; file
  names and dates are fixed by a256, so a compromised web01 can at worst replace
  *today's* daily copy (and only with files that pass the checks). Older copies
  are out of its reach.
