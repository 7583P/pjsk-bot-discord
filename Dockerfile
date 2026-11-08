FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.lock requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --uid 10001 lounge
COPY main.py app.py run.py ./
COPY cogs ./cogs
COPY helpers ./helpers
COPY lounge ./lounge
COPY assets ./assets
COPY certs ./certs
COPY migrations ./migrations
RUN ln -s /app /opt/pjsk-lounge && chown -R lounge:lounge /app
USER lounge
CMD ["python", "run.py"]
