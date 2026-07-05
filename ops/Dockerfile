# Trading-bot container (goGA GA-2.1) — the track-record substrate for a small
# always-on VPS. Build from the repo root:
#   docker build -f ops/Dockerfile -t investment-strategy .
# Run (env file stays OUTSIDE the image; state/ and logs/ persist on the host):
#   docker run -d --name bot --restart=always \
#     --env-file /path/to/.env \
#     -v "$PWD/state:/app/state" -v "$PWD/logs:/app/logs" \
#     investment-strategy
# --restart=always is the supervisor: crash, OOM, or host reboot all restart
# the bot, which then runs its reconcile-first path before trading.
FROM python:3.13-slim

WORKDIR /app

# Layer-cache the dependency install separately from the (frequently edited) code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY investment_strategy/ investment_strategy/

# Never bake secrets into the image; pass them at runtime via --env-file.
CMD ["python", "-m", "investment_strategy"]
