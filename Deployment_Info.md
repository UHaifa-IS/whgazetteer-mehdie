# Project path

/srv/www/tools.mehdie.org/app/

#settings

whg/local_settings.py

# git pull

sudo git pull origin main

# Gunicorn restart

sudo systemctl restart gunicorni-tools

# Gunicorn logs

sudo journalctl --unit=gunicorni-tools | tail -n 300
less /srv/www/tools.mehdie.org/log-app/gunicorn-error.log

# Celery restart

sudo systemctl restart celery

# Celery log

sudo journalctl --unit=gunicorni-tools | tail -n 300


# Collect static files (only adds, does not overwrite or delete)
python manage.py collectstatic
