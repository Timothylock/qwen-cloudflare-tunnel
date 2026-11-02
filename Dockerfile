FROM python:3.13-alpine
WORKDIR /app
COPY proxy.py .
USER nobody
EXPOSE 8080
# PID 1 ignores SIGTERM without a handler; SIGINT -> KeyboardInterrupt exits.
STOPSIGNAL SIGINT
HEALTHCHECK --interval=30s --timeout=3s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=2)"
CMD ["python3", "-u", "proxy.py"]
