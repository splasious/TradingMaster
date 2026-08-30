FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
