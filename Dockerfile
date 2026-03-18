FROM python:3.11-slim

WORKDIR /app

# Install OS-level deps required by Playwright and PyMuPDF
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        wget \
        gnupg \
        ca-certificates \
        libglib2.0-0 \
        libnss3 \
        libnspr4 \
        libdbus-1-3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser (Chromium only — smallest footprint)
RUN playwright install chromium

# Copy application source
COPY . .

EXPOSE 9898

CMD ["python", "larkscout_server.py"]
