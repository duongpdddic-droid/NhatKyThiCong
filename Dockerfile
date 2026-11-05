FROM python:3.11-slim
WORKDIR /app
# DejaVu fonts for Vietnamese text rendering in exported PDFs.
# Without these, reportlab falls back to Helvetica which has no
# Vietnamese glyphs and renders characters as black squares.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p uploads exports
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]