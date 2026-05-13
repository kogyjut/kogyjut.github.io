FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

# Use the browsers pre-installed in this image; skip re-download during pip install
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py .

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "10000"]
