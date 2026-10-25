FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# python-multipart: file uploads (the EU deploy script installed it on top)
RUN pip install --no-cache-dir -r requirements.txt python-multipart

COPY server.py directory.py mesh_router.py index.html ./

ENV PYTHONUNBUFFERED=1

USER 1000

EXPOSE 9009

# pager.db is opened relative to the working directory: the pod runs in /data (volume)
CMD ["uvicorn", "--app-dir", "/app", "server:app", "--host", "0.0.0.0", "--port", "9009"]
