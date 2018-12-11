# drift-apirouter
The api-router is a web server that analyses and forwards HTTP requests to different services, as well as apply access control logic.



### Nginx tuning
Advise from this [blog](https://gist.github.com/joewiz/4c39c9d061cf608cb62b) proved successful.

 - Add `fs.file-max = 70000` to `/etc/sysctl.conf`
 - Add `nginx soft nofile 10000` and `nginx hard nofile 30000` to `/etc/security/limits.conf`
 - Add `worker_rlimit_nofile 30000;` and `worker_connections 30000;` to `/etc/nginx/nginx.conf`


### UWSGI tuning

Note, the same file limit settings need to be applied to the uwsgi instance as with the nginx instance. Just use the stuff above.

Now, the important settings in uwsgi.ini are:

These paramaters have been carefully chosen and tuned to perfection:

```ini
[uwsgi]

# Process management info
# Processes should be 2 x CPU.
# Threads are not used with 'gevent'
# The number of 'gevent' doesn't seem to matter at all. Needs investigating. (Libraries are not monkeypatched, etc..)
processes = 8
threads = 1
gevent = 100
master = true
uid = ubuntu
max-worker-lifetime = 30
idle = true
pidfile = /var/run/uwsgi.pid
##thunder-lock = true

# Application info
chdir = /usr/local/bin/drift-base
wsgi-file = flask_uwsgi.py
callable  = app

# Web servers for nginx reverse proxy, local testing and uwsgitop.
# Number of listening sockets set to a rather high number.
socket = 0.0.0.0:10080
http = : 8080
stats=127.0.0.1:9191
listen = 1000

# Logging to local disk is rather uncool today. Needs rotation at least.
logto = /var/log/uwsgi/uwsgi.log

# Toggle this on to enable simple call profiling:
#profiler=pycall
```


### PGBouncer tuning

Reading material:

[How to Pool PostgreSQL Connections with PgBouncer](https://www.compose.com/articles/how-to-pool-postgresql-connections-with-pgbouncer/)

Installing is straightforward.

```bash
# Note, use /etc/pgbouncer rather than /usr/local/etc.
sudo apt-get install pgbouncer
sudo mkdir /var/log/pgbouncer
sudo mkdir /var/run/pgbouncer
sudo nano /usr/local/etc/pgbouncer.ini
sudo chown ubuntu /usr/local/etc/pgbouncer.ini
sudo chown ubuntu /var/run/pgbouncer
sudo nano /usr/local/etc/userlist.txt
sudo chown ubuntu /usr/local/etc/userlist.txt

# The contents of .ini and userlist are described elsewhere.

# Run bouncer in shell:
pgbouncer -R /usr/local/etc/pgbouncer.ini
```

The userlist.txt file is generated using a python script that comes with pgbouncer as is called something something. The file can also be made by hand.

#### The interesting parameters of pgbouncer.ini:

The databases are mapped in in a straight forward manner

```ini
[databases]
DEVNORTH_default_drift-base = host=postgres.devnorth.dg-api.com user=zzp_user password=zzp_user

[pgbouncer]
logfile = /var/log/pgbouncer/pgbouncer.log
pidfile = /var/run/pgbouncer/pgbouncer.pid
listen_addr = 0.0.0.0
listen_port = 6432
auth_file = /usr/local/etc/userlist.txt
pool_mode = transaction

max_user_connections = 300

; total number of clients that can connect
max_client_conn = 300

max_db_connections = 50

```

https://www.percona.com/live/e17/sites/default/files/slides/High%20Performance%20JSON%20-%20PostgreSQL%20vs.%20MongoDB%20-%20FileId%20-%20115573.pdf
```
postgresql.conf:

shared_buffer = 16GB
max_connections = 400
fsync = on
synchronous_commit = on
full_page_writes = off
wal_compression = off
wal_buffers = 16MB
min_wal_size = 2GB
max_wal_size = 4GB
checkpoint_completion_target = 0.9
work_mem = 33554KB
maintenance_work_mem = 2GB
wal_level=replica
```

