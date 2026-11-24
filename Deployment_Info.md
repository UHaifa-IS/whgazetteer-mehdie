# Project path

/srv/www/tools.mehdie.org/app/

#settings

whg/local_settings.py

# git pull

sudo git pull origin main

# Gunicorn restart

sudo systemctl restart gunicorn-tools

# Gunicorn logs

sudo journalctl --unit=gunicorn-tools | tail -n 300
less /srv/www/tools.mehdie.org/log-app/gunicorn-error.log

# Celery restart

sudo systemctl restart celery@whg

# Celery log

sudo journalctl --unit=celery@whg | tail -n 300


# Collect static files (only adds, does not overwrite or delete)
python manage.py collectstatic

# Python environment (venv)
The services run `app/venv/bin/python` (Python 3.10). Exact package versions are in
`requirements.lock`. Always call pip as `venv/bin/python -m pip`, and keep pip < 24.1 until Celery is upgraded to 5.x.

Rebuild the venv from the lock (e.g. after a dependency update), with rollback:
```
cd /srv/www/tools.mehdie.org/app
sudo apt install -y python3.10-venv python3-dev libpq-dev build-essential   # once; psycopg2 builds from source
sudo python3.10 -m venv venv.new
sudo venv.new/bin/python -m pip install 'pip<24.1'   # Celery 4.4.7's metadata is rejected by pip >= 24.1
sudo venv.new/bin/python -m pip install -r requirements.lock
sudo -u www-data venv.new/bin/python manage.py check            # must report no issues
sudo mv venv venv.old && sudo mv venv.new venv
sudo systemctl restart gunicorn-tools celery@whg
# check the site + gunicorn-error.log; roll back with:
#   sudo mv venv venv.new && sudo mv venv.old venv && sudo systemctl restart gunicorn-tools celery@whg
```
Delete `venv.old` once the site has run cleanly for a few days.
Audit with `pip-audit -r requirements.lock` (or GitHub Dependabot).
