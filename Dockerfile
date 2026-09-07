FROM python:3.13-alpine

WORKDIR /app
COPY proxy.py /app/proxy.py

RUN addgroup -g 10001 -S app && adduser -u 10001 -S -G app app
USER 10001:10001

ENV LISTEN_PORT=18890
EXPOSE 18890
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3   CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18890/health', timeout=2).read()"
CMD ["python", "/app/proxy.py"]
