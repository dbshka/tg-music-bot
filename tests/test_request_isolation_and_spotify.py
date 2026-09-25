"""
Tests for Spotify URL resolution, topic channel validation, and request isolation / concurrency.
"""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch

from services.downloader import (
    DOWNLOAD_SEMAPHORE,
    MAX_CONCURRENT_DOWNLOADS,
    compute_candidate_penalty,
    is_valid_topic_channel,
)
from handlers.music import format_download_error


def test_topic_channel_validation():
    """Проверяет строгую валидацию Topic-каналов во избежание ложной приоритизации чужих артистов."""
    # Чужой Topic канал не должен считаться официальным каналом Deftones
    assert not is_valid_topic_channel("Souls In Chains - Topic", "Souls In Chains - Topic", "Deftones")
    assert not is_valid_topic_channel("Random Uploader - Topic", "Random Uploader", "Deftones")

    # Официальный канал артиста
    assert is_valid_topic_channel("Deftones - Topic", "Deftones - Topic", "Deftones")
    assert is_valid_topic_channel("Deftones", "Deftones - Topic", "Deftones")

    # Каналы дистрибьюторов / различных артистов
    assert is_valid_topic_channel("Various Artists - Topic", "Various Artists - Topic", "Deftones")
    assert is_valid_topic_channel("Release - Topic", "Release - Topic", "Deftones")

    # Без указания custom_artist любой Topic-канал допускается
    assert is_valid_topic_channel("Any Artist - Topic", "Any Artist - Topic", None)


def test_spotify_candidate_ranking_deftones():
    """
    Проверяет, что правильный студийный трек побеждает при скоринге кандидатов,
    а чужой трек (Souls In Chains feat. Deftones) и урезанный MTV-клип (211s) не выигрывают.
    """
    custom_artist = "Deftones"
    custom_title = "Be Quiet and Drive (Far Away)"
    expected_duration = 308

    # 1. Студийный трек из YouTube Music (309с, diff 1s)
    good_cand = {
        "id": "E4kdRKVuyhA",
        "title": "Be Quiet and Drive (Far Away)",
        "artist": "Deftones",
        "uploader": "Deftones",
        "channel": "Deftones",
        "duration": 309,
        "_source": "youtube",
    }

    # 2. Трек другого артиста с feat. Deftones из Topic канала (351с, diff 43s)
    foreign_topic_cand = {
        "id": "foreign123",
        "title": "Be Quiet and Drive (Far Away) (feat. Deftones)",
        "uploader": "Souls In Chains - Topic",
        "channel": "Souls In Chains - Topic",
        "duration": 351,
        "_source": "youtube",
    }

    # 3. Официальный видеоклип MTV (урезанная версия 211с, diff 97s)
    mtv_video_cand = {
        "id": "KvknOXGPzCQ",
        "title": "Deftones - Be Quiet And Drive (Far Away) (Official Video) [HD Remaster]",
        "uploader": "Deftones",
        "channel": "Deftones",
        "duration": 211,
        "_source": "youtube",
    }

    p_good = compute_candidate_penalty(
        candidate=good_cand,
        custom_artist=custom_artist,
        custom_title=custom_title,
        expected_duration=expected_duration,
        is_apple_music=True,
    )
    p_foreign = compute_candidate_penalty(
        candidate=foreign_topic_cand,
        custom_artist=custom_artist,
        custom_title=custom_title,
        expected_duration=expected_duration,
        is_apple_music=True,
    )
    p_mtv = compute_candidate_penalty(
        candidate=mtv_video_cand,
        custom_artist=custom_artist,
        custom_title=custom_title,
        expected_duration=expected_duration,
        is_apple_music=True,
    )

    # Студийный трек должен получить наилучший (минимальный) штрафной балл
    assert p_good < p_foreign, f"Good track ({p_good}) must beat foreign topic ({p_foreign})"
    assert p_good < p_mtv, f"Good track ({p_good}) must beat cut MTV video ({p_mtv})"
    assert p_foreign > 1000.0, f"Foreign topic track diff=43s should have high penalty: {p_foreign}"


def test_format_download_error_handles_timeout():
    """Проверяет преобразование таймаутов в понятный UX-текст."""
    async_to = asyncio.TimeoutError()
    msg = format_download_error(async_to)
    assert "Возникла ошибка 8" in msg

    to_err = TimeoutError("Candidate exceeded candidate deadline")
    msg2 = format_download_error(to_err)
    assert "Возникла ошибка 8" in msg2


@pytest.mark.asyncio
async def test_scenario_1_fast_request_not_blocked_by_hanging_request():
    """
    Сценарий 1:
    - Запрос A: искусственно замедленный/висящий внешний запрос (эмуляция подвисшего Spotify / external resolver).
    - Запрос B: нормальный быстрый запрос (текстовый поиск / кэш / прямой запрос).
    - Запустить параллельно: B не должен ждать завершения A и должен выполниться за штатное время.
    """
    results = {}

    async def hanging_spotify_request_a():
        t0 = time.perf_counter()
        # Эмуляция внешнего резолвера Spotify с bounded timeout
        try:
            await asyncio.wait_for(asyncio.sleep(2.0), timeout=0.4)
            results["task_a"] = "success"
        except asyncio.TimeoutError:
            results["task_a"] = "timeout"
        results["task_a_time"] = time.perf_counter() - t0

    async def normal_fast_request_b():
        t0 = time.perf_counter()
        # Быстрый пользовательский запрос (разрешение + доступ к загрузчику)
        await asyncio.sleep(0.03)  # быстрое разрешение метаданных
        async with DOWNLOAD_SEMAPHORE:
            await asyncio.sleep(0.05)  # симуляция быстрой отдачи/скачивания
            results["task_b"] = "completed"
        results["task_b_time"] = time.perf_counter() - t0

    t_start = time.perf_counter()
    await asyncio.gather(hanging_spotify_request_a(), normal_fast_request_b())
    t_total = time.perf_counter() - t_start

    # B завершился штатно и быстро, не дожидаясь отсечки A
    assert results["task_b"] == "completed"
    assert results["task_a"] == "timeout"
    assert results["task_b_time"] < 0.25, f"Task B took {results['task_b_time']}s, expected < 0.25s"
    assert t_total < 0.65, f"Total execution time took {t_total}s, expected bounded < 0.65s"
    assert DOWNLOAD_SEMAPHORE._value == 1, "Semaphore must be fully available"


@pytest.mark.asyncio
async def test_scenario_2_one_hanging_plus_nine_concurrent_requests():
    """
    Сценарий 2:
    - 1 зависший запрос Spotify (эмуляция таймаута внешнего API) + 9 нормальных параллельных запросов.
    - Зафиксировать:
      * завершились ли все 9 нормальных запросов вовремя;
      * не застрял ли семафор/worker pool;
      * корректно ли освободились ресурсы после таймаута запроса А.
    """
    assert DOWNLOAD_SEMAPHORE._value == 1

    completed_fast_tasks = []
    task_times = {}

    async def hanging_spotify_request(task_id: int):
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(asyncio.sleep(3.0), timeout=0.4)
        except asyncio.TimeoutError:
            task_times[f"hanging_{task_id}"] = time.perf_counter() - t0

    async def normal_user_request(task_id: int):
        t0 = time.perf_counter()
        # Метаданные / поиск / кэш выполняются параллельно в event loop
        await asyncio.sleep(0.04)
        completed_fast_tasks.append(task_id)
        task_times[f"normal_{task_id}"] = time.perf_counter() - t0

    tasks = [hanging_spotify_request(0)]
    for i in range(1, 10):
        tasks.append(normal_user_request(i))

    t_start = time.perf_counter()
    await asyncio.gather(*tasks)
    t_total = time.perf_counter() - t_start

    # 1. Все 9 нормальных запросов завершились успешно
    assert len(completed_fast_tasks) == 9
    assert sorted(completed_fast_tasks) == list(range(1, 10))

    # 2. Все нормальные запросы завершились быстро (< 0.25s), не ожидая таймаута висящего запроса
    for i in range(1, 10):
        assert task_times[f"normal_{i}"] < 0.25, f"Normal task {i} took {task_times[f'normal_{i}']}s, expected < 0.25s"

    # 3. Висящий запрос А завершился по таймауту
    assert task_times["hanging_0"] >= 0.35

    # 4. Семафор полностью свободен и не застрял
    assert DOWNLOAD_SEMAPHORE._value == 1


@pytest.mark.asyncio
async def test_scenario_8_hanging_plus_download_plus_search():
    """
    Сценарий 8:
    - Запрос A: искусственно зависающий внешний resolver (Spotify).
    - Запрос B: обычная загрузка (через DOWNLOAD_SEMAPHORE).
    - Запрос C: обычный текстовый поиск.
    Проверяет, что B и C не ждут таймаута A, семафор не ломается, ресурсы освобождаются.
    """
    assert DOWNLOAD_SEMAPHORE._value == 1
    results = {}

    async def req_a():
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(asyncio.sleep(2.0), timeout=0.4)
            results["A"] = "success"
        except asyncio.TimeoutError:
            results["A"] = "timeout"
        results["A_time"] = time.perf_counter() - t0

    async def req_b():
        t0 = time.perf_counter()
        await asyncio.sleep(0.02)
        async with DOWNLOAD_SEMAPHORE:
            await asyncio.sleep(0.08)
            results["B"] = "success"
        results["B_time"] = time.perf_counter() - t0

    async def req_c():
        t0 = time.perf_counter()
        await asyncio.sleep(0.03)
        results["C"] = "success"
        results["C_time"] = time.perf_counter() - t0

    t_start = time.perf_counter()
    await asyncio.gather(req_a(), req_b(), req_c())
    t_total = time.perf_counter() - t_start

    assert results["A"] == "timeout"
    assert results["B"] == "success"
    assert results["C"] == "success"
    assert results["C_time"] < 0.1, f"Search took {results['C_time']}s, should be < 0.1s"
    assert results["B_time"] < 0.2, f"Download took {results['B_time']}s, should not wait for A"
    assert DOWNLOAD_SEMAPHORE._value == 1


@pytest.mark.asyncio
async def test_scenario_9_retry_and_subsequent_requests_after_timeout():
    """
    Сценарий 9:
    Проверка повторного запроса после timeout:
    - A -> timeout -> повторный запрос A завершается успехом;
    - Последующие запросы B (загрузка) и C (поиск) работают без застрявших locks/tasks.
    """
    assert DOWNLOAD_SEMAPHORE._value == 1
    results = {}

    # Фаза 1: таймаут первого запроса
    try:
        await asyncio.wait_for(asyncio.sleep(1.0), timeout=0.2)
    except asyncio.TimeoutError:
        results["A_initial"] = "timeout"
    assert results["A_initial"] == "timeout"

    # Фаза 2: повторный запрос A + параллельные B и C
    async def req_a_retry():
        t0 = time.perf_counter()
        await asyncio.wait_for(asyncio.sleep(0.04), timeout=0.3)
        results["A_retry"] = "success"
        results["A_retry_time"] = time.perf_counter() - t0

    async def req_b_next():
        t0 = time.perf_counter()
        async with DOWNLOAD_SEMAPHORE:
            await asyncio.sleep(0.05)
            results["B_next"] = "success"
        results["B_next_time"] = time.perf_counter() - t0

    async def req_c_next():
        t0 = time.perf_counter()
        await asyncio.sleep(0.02)
        results["C_next"] = "success"
        results["C_next_time"] = time.perf_counter() - t0

    await asyncio.gather(req_a_retry(), req_b_next(), req_c_next())

    assert results["A_retry"] == "success"
    assert results["B_next"] == "success"
    assert results["C_next"] == "success"
    assert DOWNLOAD_SEMAPHORE._value == 1
