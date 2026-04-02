# Polymarket Neg-Risk Arbitrage Bot

FROM python:3.11-slim

# Avoid Python buffering (important for real-time log streaming)
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies (gcc for compiled packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY core/ core/
COPY polymarket_client/ polymarket_client/
COPY kalshi_client/ kalshi_client/
COPY utils/ utils/
COPY dashboard/ dashboard/
COPY config.yaml .
COPY negrisk_long_test.py .
COPY main.py .
COPY run_with_dashboard.py .

# Create non-root user for security
RUN groupadd -r negrisk && useradd -r -g negrisk -d /app negrisk \
    && mkdir -p logs/negrisk/recordings \
    && chown -R negrisk:negrisk /app

USER negrisk

# Health check: verify Python can import and logs directory is writable
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "from core.negrisk.engine import NegriskEngine; from pathlib import Path; assert Path('logs/negrisk').exists(); print('ok')" || exit 1

# Default: run the negrisk scanner
# Override CMD in docker-compose for different configurations
ENTRYPOINT ["python", "-u", "negrisk_long_test.py"]
CMD ["--edge", "0.5", "--staleness", "300", "--gamma-legs", "2", "--record", "--duration", "720"]
