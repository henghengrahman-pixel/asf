FROM mcr.microsoft.com/playwright:v1.55.0-noble
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-pip curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt
ENV PATH="/opt/venv/bin:$PATH"
COPY dp-service/package.json /app/dp-service/package.json
RUN cd /app/dp-service && npm install --omit=dev
COPY . /app
RUN chmod +x /app/start.sh
CMD ["/app/start.sh"]
