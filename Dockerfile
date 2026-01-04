FROM python:3.10
WORKDIR /app
COPY . .

# 🛠️ INSTALL FFMPEG (The Missing Ingredient)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "main.py"]
