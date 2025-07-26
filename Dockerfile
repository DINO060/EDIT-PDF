# Multi-stage build for production optimization
FROM python:3.11-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip wheel setuptools && \
    pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim AS production

# Install runtime dependencies for PDF processing
RUN apt-get update && apt-get install -y \
    wkhtmltopdf \
    xvfb \
    libmagic1 \
    libxrender1 \
    libxext6 \
    libxtst6 \
    libfontconfig1 \
    libxss1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-root user for security
RUN groupadd -r pdfbot && useradd -r -g pdfbot -u 10000 pdfbot

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=pdfbot:pdfbot . .

# Create necessary directories
RUN mkdir -p data/temp logs && \
    chown -R pdfbot:pdfbot data logs

# Copy health check script
COPY docker/healthcheck.sh /usr/local/bin/healthcheck.sh
RUN chmod +x /usr/local/bin/healthcheck.sh

# Copy entrypoint script
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Switch to non-root user
USER pdfbot

# Expose health check port (if needed)
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD /usr/local/bin/healthcheck.sh

# Set entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Default command
CMD ["python", "main.py"]

# Labels for metadata
LABEL maintainer="PDF Bot Team" \
      version="1.0.0" \
      description="Production-ready Telegram PDF processing bot" \
      org.opencontainers.image.source="https://github.com/your-repo/telegram-pdf-bot" 