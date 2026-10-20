FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Sin root: una ejecución de código en la app no da root dentro del contenedor.
RUN useradd --system --uid 1001 --no-create-home app \
    && chown -R app /app
USER app

EXPOSE 8000
# main:socket_app y NO main:app: con `app` la capa Socket.IO (realtime.py)
# no se monta, /socket.io/* cae en FastAPI (404) y el WebSocket de alertas
# se rechaza con 403 en cada carga de página.
CMD ["uvicorn", "main:socket_app", "--host", "0.0.0.0", "--port", "8000"]
