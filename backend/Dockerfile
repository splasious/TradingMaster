FROM python:3.12-slim

WORKDIR /app

# git is required by pip to install neo_api_client (Kotak Neo's broker
# SDK, requirements.txt) from its GitHub repo -- it has no PyPI release,
# and python:3.12-slim doesn't include git by default.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# --no-deps: neo_api_client's own setup.py hard-pins asyncio==3.4.3 (fails
# to install on Python 3.12) among other stale exact versions -- its real
# dependencies are already satisfied by requirements.txt above instead.
RUN pip install --no-cache-dir --no-deps "neo_api_client @ git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2"

COPY . .

EXPOSE 8000

# Migrations and seeding run on every boot, not just first deploy -- both
# are idempotent (alembic tracks its own revision, seed.py checks for
# existing rows), so this is safe to repeat and means a fresh Render/Docker
# deploy always ends up with an up-to-date schema without a separate manual
# step. docker-compose.yml (local dev) does the same thing as an override
# command; this makes it the image's actual default so single-container
# platforms like Render get it too.
CMD ["sh", "-c", "alembic upgrade head && python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
