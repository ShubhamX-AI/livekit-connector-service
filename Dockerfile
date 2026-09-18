# Runtime for the standalone Google Meet connector.
# Chrome and ChromeDriver are pinned to the same versions as the parent
# attendee image, and ChromeDriver is installed at /usr/local/bin/chromedriver,
# which is the default of ConnectorConfig.chrome_driver_path.
FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
        unzip \
        python3 \
        python3-pip \
        xvfb \
        xauth \
        x11-xkb-utils \
        fonts-liberation \
        libvulkan1 \
        xdg-utils \
        libasound2 \
        libasound2-plugins \
        alsa-utils \
        pulseaudio \
        pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

# Chrome 134.0.6998.88, from the same mirror and with the same checksum as the
# parent image.
RUN wget --progress=dot:giga --timeout=30 --tries=3 \
        https://build-assets.attendee.dev/google-chrome/pool/main/g/google-chrome-stable/google-chrome-stable_134.0.6998.88-1_amd64.deb \
    && echo "df557edb3d24d8dcaff9557d80733b42afb6626685200d3f34a3b6f528065cad  google-chrome-stable_134.0.6998.88-1_amd64.deb" | sha256sum -c - \
    && apt-get update \
    && apt-get install -y --no-install-recommends ./google-chrome-stable_134.0.6998.88-1_amd64.deb \
    && rm -rf google-chrome-stable_134.0.6998.88-1_amd64.deb /var/lib/apt/lists/*

# Matching ChromeDriver.
RUN wget -q https://storage.googleapis.com/chrome-for-testing-public/134.0.6998.88/linux64/chromedriver-linux64.zip \
    && echo "58df717d51484b9f3ac188af5231cdc77255daa72d0b2b86481bee54e398ce2f  chromedriver-linux64.zip" | sha256sum -c - \
    && unzip -q chromedriver-linux64.zip \
    && mv chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf chromedriver-linux64 chromedriver-linux64.zip

WORKDIR /app

# Dependencies first so that source edits do not invalidate the layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY assets ./assets
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Chrome refuses to run as root without a sandbox opt-out; run as a normal user.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "-m", "standalone_google_meet"]
