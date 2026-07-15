# Base image ships Chromium + all its system libs preinstalled, matching the
# playwright version pinned in requirements.txt - avoids hand-rolling the long
# list of apt packages Playwright's browsers need to run headless in a container.
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# Copy requirements before the rest of the source so Docker's layer cache can
# skip the pip install step on rebuilds where only application code changed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "discord_bot.py"]
