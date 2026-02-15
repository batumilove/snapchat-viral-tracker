FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask requests pytest
COPY app.py .
COPY test_app.py .
VOLUME /data
EXPOSE 5000
CMD ["python", "app.py"]
