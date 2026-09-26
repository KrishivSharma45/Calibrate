FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv/src \
    FIXTURES_PATH=/srv/fixtures.json

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fixtures.json .
COPY src ./src

# Run as a non-root user.
RUN useradd --create-home --uid 1000 portal
USER portal

EXPOSE 8080
CMD ["python", "-m", "app.boot"]
