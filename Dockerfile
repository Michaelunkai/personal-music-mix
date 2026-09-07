FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY frontend ./frontend
COPY extension ./extension
COPY .env.example README.md ./
ENV YTMUSIC_RECOMMENDER_HOST=0.0.0.0
ENV YTMUSIC_RECOMMENDER_PORT=8000
EXPOSE 8000
CMD ["python", "-m", "app.main"]
