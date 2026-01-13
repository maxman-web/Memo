FROM python:3.10

# 1. Setup User (Hugging Face requires User ID 1000)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# 2. Setup Working Directory
WORKDIR $HOME/app

# 3. Copy Files with Permissions
COPY --chown=user . $HOME/app

# 4. Install FFMPEG (As Root, then switch back)
USER root
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*
USER user

# 5. Install Python Requirements
RUN pip install --no-cache-dir -r requirements.txt

# 6. Run the Bot
CMD ["python", "main.py"]
