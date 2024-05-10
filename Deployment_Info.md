# Project path

/home/macbookpro/whgazetteer-mehdie

# git pull

sudo git pull origin main

# Gunicorn restart

sudo systemctl restart gunicorn.service

# Gunicorn log

sudo journalctl --unit=gunicorn | tail -n 300

# Celery restart

sudo supervisorctl restart celery

# Celery log

sudo supervisorctl tail -10000 celery stderr

# Celery config

sudo vi /etc/supervisor/conf.d/celery.conf

# Collect static files (only adds, does not overwrite or delete)
python manage.py collectstatic