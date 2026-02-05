FROM python:3.11-slim

LABEL maintainer="MotoGParr"
LABEL description="MotoGP Automated Recording & Retrieval"

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY utils/ ./utils/
COPY config/ ./config/
COPY assets/ ./assets/
COPY motogp_common.py .
COPY motogp_grabber.py .
COPY motogp_sort.py .
COPY motogp_scheduler.py .
COPY motogp_watcher.py .
COPY motogp_discord.py .
COPY motogp_plex_tagger.py .
COPY motogp_overlay_utility.py .
COPY motogp_webui.py .
COPY grab_stats.py .
COPY activity_emitter.py .
COPY templates/ ./templates/
COPY static/ ./static/
COPY entrypoint.sh .

RUN chmod +x entrypoint.sh *.py

ENV PYTHONUNBUFFERED=1
ENV CONFIG_DIR=/config
ENV TZ=Europe/London

VOLUME ["/config"]

EXPOSE 5679

ENTRYPOINT ["/app/entrypoint.sh"]