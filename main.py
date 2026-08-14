from aiohttp import web
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    FSInputFile,
    InputMediaAudio,
    InputMediaPhoto,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

MAX_RESULTS = 5
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB лимит Telegram Bot API

dp = Dispatcher()


@dataclass
class Track:
    track_name: str
    artist_name: str
    collection_name: Optional[str]
    artwork_url: Optional[str]
    preview_url: Optional[str]
    track_time_millis: Optional[int]
    track_view_url: Optional[str]

    @classmethod
    def from_itunes(cls, data: Dict[str, Any]) -> "Track":
        return cls(
            track_name=data.get("trackName", "Без названия"),
            artist_name=data.get("artistName", "Неизвестный исполнитель"),
            collection_name=data.get("collectionName"),
            artwork_url=data.get("artworkUrl100"),
            preview_url=data.get("previewUrl"),
            track_time_millis=data.get("trackTimeMillis"),
            track_view_url=data.get("trackViewUrl"),
        )


def shorten(text: Optional[str], limit: int = 64) -> Optional[str]:
    """
    Обрезает строку до лимита Telegram.
    """
    if not text:
        return None

    text = str(text).strip()

    if len(text) <= limit:
        return text

    return text[: limit - 1] + "…"


def format_duration(millis: Optional[int]) -> Optional[int]:
    """
    Переводит миллисекунды в секунды для Telegram.
    """
    if not millis:
        return None

    try:
        seconds = int(millis) // 1000
        return seconds if seconds > 0 else None
    except (TypeError, ValueError):
        return None


def build_caption(track: Track, preview: bool = False) -> str:
    """
    Собирает красивый caption.
    """
    parts = [f"{track.artist_name} — {track.track_name}"]

    if track.collection_name:
        parts.append(f"Альбом: {track.collection_name}")

    if preview:
        parts.append("(30-секундное превью)")

    if track.track_view_url:
        parts.append(f"Слушать полностью: {track.track_view_url}")

    return "\n".join(parts)


async def itunes_search(
    session: aiohttp.ClientSession,
    query: str,
    limit: int = MAX_RESULTS,
) -> List[Track]:
    """
    Ищет треки через iTunes Search API.
    """
    params = {
        "term": query,
        "media": "music",
        "entity": "song",
        "limit": limit,
    }

    try:
        async with session.get(ITUNES_SEARCH_URL, params=params) as resp:
            if resp.status != 200:
                logging.error("iTunes API error %s", resp.status)
                return []

            data = await resp.json(content_type=None)
            results = data.get("results", [])

            tracks: List[Track] = []

            for item in results:
                track = Track.from_itunes(item)

                # Оставляем только треки с превью
                if not track.preview_url:
                    continue

                tracks.append(track)

            return tracks

    except Exception:
        logging.exception("Ошибка запроса к iTunes")
        return []


async def download_artwork(
    session: aiohttp.ClientSession,
    url: str,
) -> Optional[bytes]:
    """
    Скачивает обложку.
    """
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception:
        logging.exception("Не удалось скачать обложку")

    return None


async def send_preview(message: Message, track: Track, session: aiohttp.ClientSession) -> bool:
    """
    Отправляет превью трека пользователю.
    Сначала пробует отправить как аудио,
    если не получится - как голосовое сообщение или файл.
    """
    if not track.preview_url:
        return False

    caption = build_caption(track, preview=True)

    audio_kwargs = {
        "title": shorten(track.track_name, 64),
        "performer": shorten(track.artist_name, 64),
        "duration": format_duration(track.track_time_millis),
        "caption": caption,
    }

    # 1. Пробуем отправить прямую ссылку
    try:
        await message.answer_audio(
            audio=track.preview_url,
            **audio_kwargs,
        )
        return True
    except Exception as exc:
        logging.warning("Не удалось отправить аудио по URL: %s", exc)

    # 2. Если не получилось, отправляем обложку и текст
    if track.artwork_url:
        artwork = await download_artwork(session, track.artwork_url)

        if artwork:
            try:
                await message.answer_photo(
                    photo=artwork,
                    caption=caption,
                )
                return True
            except Exception as exc:
                logging.warning("Не удалось отправить фото: %s", exc)

    # 3. Крайний вариант - просто текст
    try:
        await message.answer(caption)
        return True
    except Exception:
        return False


async def search_and_send(message: Message, query: str) -> None:
    """
    Ищет треки и отправляет результаты.
    """
    query = query.strip()

    if not query:
        return

    await message.bot.send_chat_action(
        chat_id=message.chat.id,
        action="typing",
    )

    async with aiohttp.ClientSession() as session:
        tracks = await itunes_search(session, query, MAX_RESULTS)

    if not tracks:
        await message.answer(
            "Не нашёл трек. Попробуй изменить запрос или написать по-другому."
        )
        return

    for track in tracks[:MAX_RESULTS]:
        async with aiohttp.ClientSession() as session:
            await send_preview(message, track, session)


@dp.message(CommandStart())
async def command_start(message: Message) -> None:
    await message.answer(
        "Привет! Я SongDrop Bot.\n\n"
        "Напиши название и автора, например:\n"
        "Imagine Dragons Believer\n\n"
        "Я найду трек через iTunes и отправлю превью (30 секунд).\n\n"
        "Команды:\n"
        "/find Название Автор - поиск"
    )


@dp.message(Command("find"))
async def command_find(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)

    if len(parts) < 2:
        await message.answer("Использование: /find Название Автор")
        return

    await search_and_send(message, parts[1])


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    """
    Обрабатывает обычный текст в личных сообщениях.
    """
    if message.chat.type != "private":
        return

    await search_and_send(message, message.text or "")


async def main() -> None:
    import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

MAX_RESULTS = 5

dp = Dispatcher()


@dp.message(CommandStart())
async def command_start(message: Message) -> None:
    await message.answer(
        "Привет! Я SongDrop Bot.\n\n"
        "Напиши название и автора, например:\n"
        "Imagine Dragons Believer"
    )


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    await message.answer(f"Ты написал: {message.text}")


async def main() -> None:
async def health(request):
    """Страница-заглушка, чтобы Render считал сервис живым."""
    return web.Response(text="OK")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    if not BOT_TOKEN:
        raise SystemExit("Укажи BOT_TOKEN в переменных окружения Render")

    # Настройка прокси (на Render прокси НЕ нужен, PROXY_URL будет пустой)
    session = None

    if PROXY_URL:
        logging.info("Используем прокси: %s", PROXY_URL)
        try:
            session = AiohttpSession(proxy=PROXY_URL)
            session._connector_init.update({"ssl": False})
        except Exception as exc:
            logging.error("Не удалось настроить прокси: %s", exc)
            session = None

    bot = Bot(token=BOT_TOKEN, session=session)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Помощь"),
            BotCommand(command="find", description="Найти трек"),
        ]
    )

    await bot.delete_webhook(drop_pending_updates=True)

    # Мини-сервер для Render: отвечает "OK" на проверки здоровья
    app = web.Application()
    app.router.get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Health-check сервер запущен на порту %s", port)

    # Запуск самого бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())