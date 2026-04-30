FROM python:3.11-slim

WORKDIR /app

# Install OS-level deps required by Playwright, PyMuPDF, and legacy Office conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        wget \
        gnupg \
        ca-certificates \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
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
        libxext6 \
        libxfixes3 \
        libxrandr2 \
        libxrender1 \
        libgbm1 \
        libasound2 \
        libreoffice-writer \
        libreoffice-impress \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt requirements-ocr-linux-x86_64.txt requirements-ocr-arm64.txt ./
COPY scripts/install_ocr_deps.sh ./scripts/install_ocr_deps.sh
RUN pip install --no-cache-dir -r requirements.txt \
    && sh ./scripts/install_ocr_deps.sh

# Install Playwright browser (Chromium only — smallest footprint)
RUN playwright install chromium

# Copy application source
COPY . .

EXPOSE 9898

CMD ["python", "larkscout_server.py"]
