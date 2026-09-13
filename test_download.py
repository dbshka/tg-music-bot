import asyncio
from services.extractor import resolve_track_url, find_first_url
from services.downloader import download_track


async def main():
    test_urls = [
        # Spotify link
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        # SoundCloud / YouTube search test
        "ytsearch1:Rick Astley Never Gonna Give You Up"
    ]

    print("=== ТЕСТ ИЗВЛЕЧЕНИЯ ССЫЛКИ SPOTIFY ===")
    spotify_url = test_urls[0]
    track_info = await resolve_track_url(spotify_url)
    print(f"Платформа: {track_info.platform}")
    print(f"Цель для yt-dlp: {track_info.target}")
    print(f"Распознанный заголовок: {track_info.title}")
    print(f"Распознанный артист: {track_info.artist}")
    print(f"Поиск: {track_info.is_search}")

    print("\n=== ТЕСТ ЗАГРУЗКИ И КОНВЕРТАЦИИ В MP3 ===")
    print(f"Скачивание: {track_info.target}...")
    audio = await download_track(
        query_or_url=track_info.target,
        custom_title=track_info.title,
        custom_artist=track_info.artist
    )

    print(f"Файл успешно создан: {audio.file_path}")
    print(f"Размер: {audio.filesize / 1024 / 1024:.2f} МБ")
    print(f"Длительность: {audio.duration} сек")
    print(f"Теги: {audio.artist} - {audio.title}")
    print(f"Обложка: {audio.thumbnail_path}")

    # Очистка
    audio.cleanup()
    print("Временные файлы очищены успешно!")


if __name__ == "__main__":
    asyncio.run(main())
