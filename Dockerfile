FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd -r -m appuser
COPY app.py .
COPY test_app.py .
RUN chown -R appuser:appuser /app
USER appuser
VOLUME /data
EXPOSE 5000
CMD ["python", "app.py"]
