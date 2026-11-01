FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --create-home lounge
COPY main.py app.py ./
COPY lounge ./lounge
COPY cogs ./cogs
COPY public ./public
USER lounge
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health/ready',timeout=3)"
CMD ["python", "main.py"]
