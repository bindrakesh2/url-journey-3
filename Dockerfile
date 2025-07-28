# Start with a modern Python version
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies required by Playwright's browsers
RUN apt-get update && apt-get install -y \
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
    --no-install-recommends

# Copy and install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the Chromium browser for Playwright
RUN playwright install chromium

# Copy all your application code into the container
COPY . .

# Make the startup script executable
RUN chmod +x ./start.sh

# Set the startup script as the container's entry point
CMD ["./start.sh"]
