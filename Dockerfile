FROM python:3.11-slim

ARG TZ
ENV TZ="$TZ"

RUN apt-get update && apt-get install -y --no-install-recommends \
  git \
  procps \
  curl \
  ca-certificates \
  unzip \
  jq \
  vim \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY . /workspace

RUN python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt

# Run as non-root user for security
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /workspace
USER appuser

ENV STREAMLIT_SERVER_PORT=8009
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 8009
RUN mkdir -p /workspace/output /workspace/data/uploads

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8009/_stcore/health || exit 1

CMD ["streamlit", "run", "patent_writer_app.py"]
