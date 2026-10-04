"""Gunicorn configuration (used inside the container)."""

import os

bind = os.environ.get("DOCNEST_BIND", "0.0.0.0:8000")
workers = int(os.environ.get("DOCNEST_WEB_WORKERS", "2"))
threads = int(os.environ.get("DOCNEST_WEB_THREADS", "4"))
worker_class = "gthread"
timeout = 300  # downloads from Proton Drive can take a while
graceful_timeout = 30
keepalive = 5
accesslog = None  # access logs would contain URLs (document IDs, search queries)
errorlog = "-"
loglevel = "warning"
forwarded_allow_ips = os.environ.get("DOCNEST_FORWARDED_ALLOW_IPS", "*")
limit_request_line = 8190
control_socket_disable = True  # gunicorn's control socket is not needed (read-only filesystem)
