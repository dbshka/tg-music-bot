FROM python:3.11-slim

# Установка системных утилит, FFmpeg и Node.js
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Добавление Deno (нативный JS-рантайм yt-dlp по умолчанию для решения челленджей YouTube)
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

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

