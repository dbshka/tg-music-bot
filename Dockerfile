FROM python:3.11-slim

# Установка системных утилит и FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Создание пользователя с UID 1000 (стандарт безопасности Hugging Face Spaces)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Установка зависимостей Python
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода бота
COPY --chown=user . .

# Создание папки для временных файлов с полными правами доступа
RUN mkdir -p downloads && chmod -R 777 $HOME/app

# Порт для Hugging Face Spaces (Healthcheck сервер)
EXPOSE 7860
ENV PORT=7860

# Запуск бота
CMD ["python", "bot.py"]

