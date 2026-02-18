FROM python:3.11-slim

ARG TZ
ENV TZ="$TZ"

# Install system tools
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

# Copy project files
COPY . /workspace

# Install Python dependencies
RUN python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt

# Set Streamlit environment variables
ENV STREAMLIT_SERVER_PORT=8009
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Expose Streamlit port
EXPOSE 8009

# Ensure runtime directories exist
RUN mkdir -p /workspace/output /workspace/data/uploads

# Start web app
CMD ["streamlit", "run", "patent_writer_app.py"]
