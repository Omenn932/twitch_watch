FROM python:3.12-slim

WORKDIR /app

COPY twitch/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY twitch/twitch_watch.py /app/twitch_watch.py

CMD ["python", "-u", "twitch_watch.py"]
