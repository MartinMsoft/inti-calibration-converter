FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py extraction.py validation.py excel_export.py ./
COPY .streamlit/ .streamlit/

EXPOSE 8501

CMD ["sh", "-c", "streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true --server.fileWatcherType=none"]
