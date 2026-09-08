FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kickboard/ kickboard/
COPY config.yaml .

# sim/debug UI + /health
EXPOSE 8771
# radar frames in from the radar node
EXPOSE 4049/udp

CMD ["python", "-m", "kickboard.main", "--config", "/data/config.yaml"]
