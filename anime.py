import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, BufferedInputFile, CallbackQuery, InputMediaPhoto, InlineKeyboardButton, \
    InlineKeyboardMarkup, KeyboardButtonRequestChat
from aiogram.utils.keyboard import InlineKeyboardBuilder
import re

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
import uuid
import os
import aiomysql
import asyncio
import logging
import cv2
import subprocess

from dotenv import load_dotenv
from PicImageSearch import Network, Yandex
from PicImageSearch.model import YandexResponse
from anime_parsers_ru import ShikimoriParserAsync
import random

# Настройка логгера
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

parser = ShikimoriParserAsync()

load_dotenv()

BOT_TOKEN = os.getenv("ANIME_BOT")
ADMIN_ID = os.getenv("ADMIN_ID")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
BOT_NAME = "Аниме со скриншота"

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "db": "anime",
}


class AdminStates(StatesGroup):
    waiting_for_contact_message = State()

class AnimeSearchStates(StatesGroup):
    waiting_for_anime_name = State()

class CancelTypes(StatesGroup):
    CANCEL_ANIME_SEARCH = "cancel_type:anime_search"
    CANCEL_ADMIN_MESSAGE = "cancel_type:no_send_admin"


async def create_pool():
    return await aiomysql.create_pool(**DB_CONFIG)


async def init_db():
    """Создает таблицу users, если она не существует"""
    async with await create_pool() as pool:
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        username VARCHAR(255),
                        first_name VARCHAR(255),
                        last_name VARCHAR(255),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await conn.commit()


async def register_user(user_id: int, username: str, first_name: str, last_name: str):
    async with await create_pool() as pool:
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "INSERT INTO users (user_id, username, first_name, last_name) "
                    "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
                    "username = VALUES(username), first_name = VALUES(first_name), last_name = VALUES(last_name)",
                    (user_id, username, first_name, last_name)
                )
                await conn.commit()


def share_bot():
    markup = InlineKeyboardBuilder()
    markup.button(
        text="Поделиться в чате 🚀",
        switch_inline_query=" – бот поиска аниме по скриншоту"
    )
    markup.button(
        text="Задонатить 💰",
        url="https://yoomoney.ru/to/410018587631465"
    )
    markup.adjust(1)
    return markup.as_markup()


def actions_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Задонатить", callback_data="donate", url="https://yoomoney.ru/to/410018587631465")
    builder.button(
        text="Связаться с админом",
        callback_data="contact_admin"
    )
    builder.adjust(1)
    return builder.as_markup()




@dp.message(Command("start"))
async def cmd_start(message: Message):
    await register_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name
    )

    await message.answer(
        f"Добро пожаловать в {BOT_NAME}!\n\n"
        "Отправьте мне скриншот/ссылку на ролик в тикток, ютуб шортс и я покажу возможные совпадения.\n\n"
        "Для поиска информации об аниме используйте команду /anime"
    )

    pinned_msg = await message.answer(
        "Не забывай заглядывать сюда 👇",
        reply_markup=actions_keyboard()
    )

    try:
        chat = await bot.get_chat(message.chat.id)
        if chat.pinned_message:
            await bot.unpin_chat_message(message.chat.id)
        await bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=pinned_msg.message_id,
            disable_notification=True
        )
    except Exception as e:
        logger.error(f"Не удалось закрепить сообщение: {e}")


async def process_image(file_path: str) -> YandexResponse:
    try:
        async with Network() as client:
            yandex = Yandex(client=client)
            resp = await yandex.search(file=file_path)
            return resp
    except Exception as e:
        logger.error(f"Error in Yandex search: {e}")
        return None


def create_pagination_keyboard(search_url: str, current_page: int, total_pages: int):
    builder = InlineKeyboardBuilder()

    if total_pages > 1:
        if current_page > 1:
            builder.button(text="⬅️ Назад", callback_data=f"page_{current_page - 1}")
        if current_page < total_pages:
            builder.button(text="Вперед ➡️", callback_data=f"page_{current_page + 1}")

    builder.button(text="🔍 Все результаты", url=search_url)

    builder.adjust(2, 1)
    return builder.as_markup()


async def send_result_page(message: Message, resp: YandexResponse, page: int = 1, items_per_page: int = 3,
                           edit_message_id: int = None):
    if not resp or not resp.raw:
        if edit_message_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=edit_message_id,
                    text="❌ Не удалось определить аниме. Попробуйте другой скриншот."
                )
            except:
                await message.answer("❌ Не удалось определить аниме. Попробуйте другой скриншот.")
        else:
            await message.answer("❌ Не удалось определить аниме. Попробуйте другой скриншот.")
        return

    total_results = len(resp.raw)
    total_pages = (total_results + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * items_per_page
    end_idx = min(start_idx + items_per_page, total_results)

    media_group = []
    results_text = f"🔍 Результаты поиска (страница {page}/{total_pages}):\n\n"

    for i, result in enumerate(resp.raw[start_idx:end_idx], start=start_idx + 1):
        if result.title:
            title_parts = re.split(r'[-–—]', result.title)
            clean_title = title_parts[0].strip()
            original_title = result.title.strip()
        else:
            clean_title = 'Без названия'
            original_title = 'Без названия'

        results_text += (
            f"<b>Результат #{i}</b>\n"
            f"Оригинал: <code>{original_title}</code>\n"
            f"Чистое: <code>{clean_title}</code>\n"
            f"🔗 <a href='{result.url}'>Источник</a>\n\n"
        )

        if result.thumbnail:
            media_group.append(InputMediaPhoto(
                media=result.thumbnail,
                caption=f"Результат #{i} | Страница {page}/{total_pages}",
                parse_mode=ParseMode.HTML
            ))

    results_text += "\n\n<blockquote><b>Скопировать название можно нажатием\nДля поиска аниме по названию используйте /anime</b></blockquote>"

    try:
        if len(media_group) > 1:
            if edit_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=edit_message_id,
                        text=results_text,
                        reply_markup=create_pagination_keyboard(resp.url, page, total_pages),
                        disable_web_page_preview=True
                    )
                except:
                    edit_message_id = None

            if not edit_message_id:
                try:
                    # Добавляем задержку перед отправкой медиагруппы
                    await asyncio.sleep(1)  # 1 секунда задержки
                    await message.answer_media_group(media_group)
                    msg = await message.answer(
                        results_text,
                        reply_markup=create_pagination_keyboard(resp.url, page, total_pages),
                        disable_web_page_preview=True
                    )
                    return msg.message_id
                except TelegramRetryAfter as e:
                    # Обработка флуд-контроля
                    retry_after = e.retry_after
                    await message.answer(f"⚠️ Слишком быстро! Подождите {retry_after} секунд...")
                    await asyncio.sleep(retry_after)
                    return await send_result_page(message, resp, page, items_per_page, edit_message_id)

        elif media_group:
            if edit_message_id:
                try:
                    await message.bot.edit_message_media(
                        chat_id=message.chat.id,
                        message_id=edit_message_id,
                        media=InputMediaPhoto(
                            media=media_group[0].media,
                            caption=results_text,
                            parse_mode=ParseMode.HTML
                        ),
                        reply_markup=create_pagination_keyboard(resp.url, page, total_pages)
                    )
                    return edit_message_id
                except:
                    edit_message_id = None

            if not edit_message_id:
                try:
                    msg = await message.answer_photo(
                        photo=media_group[0].media,
                        caption=results_text,
                        reply_markup=create_pagination_keyboard(resp.url, page, total_pages),
                        parse_mode=ParseMode.HTML
                    )
                    return msg.message_id
                except TelegramRetryAfter as e:
                    retry_after = e.retry_after
                    await message.answer(f"⚠️ Слишком быстро! Подождите {retry_after} секунд...")
                    await asyncio.sleep(retry_after)
                    return await send_result_page(message, resp, page, items_per_page, edit_message_id)

        else:
            if edit_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=edit_message_id,
                        text=results_text,
                        reply_markup=create_pagination_keyboard(resp.url, page, total_pages),
                        disable_web_page_preview=True
                    )
                    return edit_message_id
                except:
                    edit_message_id = None

            if not edit_message_id:
                try:
                    msg = await message.answer(
                        results_text,
                        reply_markup=create_pagination_keyboard(resp.url, page, total_pages),
                        disable_web_page_preview=True
                    )
                    return msg.message_id
                except TelegramRetryAfter as e:
                    retry_after = e.retry_after
                    await message.answer(f"⚠️ Слишком быстро! Подождите {retry_after} секунд...")
                    await asyncio.sleep(retry_after)
                    return await send_result_page(message, resp, page, items_per_page, edit_message_id)

    except Exception as e:
        logger.error(f"Error sending results: {e}")
        if edit_message_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=edit_message_id,
                    text="Произошла ошибка при отправке результатов. Вот текстовая версия:\n\n" + results_text,
                    reply_markup=create_pagination_keyboard(resp.url, page, total_pages),
                    disable_web_page_preview=True
                )
                return edit_message_id
            except:
                pass

        msg = await message.answer(
            "Произошла ошибка при отправке результатов. Вот текстовая версия:\n\n" + results_text,
            reply_markup=create_pagination_keyboard(resp.url, page, total_pages),
            disable_web_page_preview=True
        )
        return msg.message_id





@dp.callback_query(F.data.startswith("page_"))
async def handle_pagination(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state == AnimeSearchStates.waiting_for_anime_name:
        await callback.answer("Закончите ввод названия аниме или отмените поиск кнопкой у сообщения", show_alert=True)
        return

    page = int(callback.data.split("_")[1])
    data = await state.get_data()
    resp = data.get("yandex_response")
    last_message_id = data.get("last_message_id")

    if resp:
        new_message_id = await send_result_page(
            callback.message,
            resp,
            page,
            edit_message_id=last_message_id
        )

        if new_message_id and new_message_id != last_message_id:
            await state.update_data(last_message_id=new_message_id)

    await callback.answer()



@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    """Обработчик фотографий"""
    await message.answer("Идет обработка изображения...")

    temp_image_path = None
    try:
        photo = message.photo[-1]
        file_id = photo.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        temp_image_path = f"temp/photo_{uuid.uuid4()}.jpg"
        await bot.download_file(file_path, temp_image_path)

        resp = await process_image(temp_image_path)

        if resp:
            await state.update_data(yandex_response=resp)
            await send_result_page(message, resp)

            await message.answer(
                "❤ Понравился бот?\n\nПоделись им с другом или знакомым 🤗",
                reply_markup=share_bot()
            )
        else:
            await message.answer("❌ Не удалось обработать изображение. Попробуйте другой скриншот.")

    except Exception as e:
        logger.error(f"Error processing photo: {e}")
        await message.answer(f"Произошла ошибка при обработке фото: {e}")
    finally:
        if temp_image_path and os.path.exists(temp_image_path):
            os.remove(temp_image_path)


async def download_youtube_shorts(url: str) -> str:
    """Скачивает YouTube Shorts через VPN и возвращает путь к файлу"""
    try:
        video_path = f"temp/youtube_shorts_{uuid.uuid4()}.mp4"

        command = [
            "yt-dlp",
            "--proxy", "socks5://127.0.0.1:10808",  # Используем тот же прокси, что и для TikTok
            "-f", "best",
            "-o", video_path,
            url
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(f"yt-dlp error for YouTube Shorts: {stderr.decode()}")
            return None

        return video_path if os.path.exists(video_path) else None
    except Exception as e:
        logger.error(f"Error downloading YouTube Shorts: {e}")
        return None

async def download_tiktok_video(url: str) -> None:
    """Скачивает видео с TikTok через VPN и возвращает путь к файлу"""
    try:
        video_path = f"temp/tiktok_{uuid.uuid4()}.mp4"

        command = [
            "yt-dlp",
            "--proxy", "socks5://127.0.0.1:10808",
            "-f", "best",
            "-o", video_path,
            url
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(f"yt-dlp error: {stderr.decode()}")
            return None

        return video_path if os.path.exists(video_path) else None
    except Exception as e:
        logger.error(f"Error downloading TikTok video: {e}")
        return None


async def extract_first_frame(video_path: str, max_attempts: int = 10) -> str:
    """Извлекает первый непустой кадр"""
    try:
        cap = cv2.VideoCapture(video_path)

        for i in range(max_attempts):
            ret, frame = cap.read()
            if not ret:
                break

            if cv2.mean(frame)[0] > 10:
                image_path = f"temp/frame_{uuid.uuid4()}.jpg"
                cv2.imwrite(image_path, frame)
                cap.release()
                return image_path

        cap.release()
        return None
    except Exception as e:
        logger.error(f"Frame extraction error: {e}")
        return None


@dp.message(F.text.contains("youtube.com/shorts/") | F.text.contains("youtu.be/"))
async def handle_youtube_shorts(message: Message, state: FSMContext):
    await message.answer("Скачиваю...")

    video_path = None
    frame_path = None

    try:
        video_path = await download_youtube_shorts(message.text)
        if not video_path:
            await message.answer("Не удалось скачать видео. Проверьте ссылку.")
            return

        await message.answer("Извлекаю первый кадр...")

        frame_path = await extract_first_frame(video_path)
        if not frame_path:
            await message.answer("Не удалось извлечь кадр из видео.")
            return

        resp = await process_image(frame_path)
        if resp:
            await state.update_data(yandex_response=resp)
            await send_result_page(message, resp)
        else:
            await message.answer("❌ Не удалось обработать кадр из видео.")

    except Exception as e:
        logger.error(f"Error processing YouTube Shorts: {e}")
        await message.answer(f"Ошибка: {str(e)}")
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
        if frame_path and os.path.exists(frame_path):
            os.remove(frame_path)

@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok_url(message: Message, state: FSMContext):
    await message.answer("Скачиваю видео из TikTok...")

    video_path = None
    frame_path = None

    try:
        video_path = await download_tiktok_video(message.text)
        if not video_path:
            await message.answer("Не удалось скачать видео. Проверьте ссылку.")
            return

        await message.answer("Извлекаю первый кадр...")

        frame_path = await extract_first_frame(video_path)
        if not frame_path:
            await message.answer("Не удалось извлечь кадр из видео.")
            return

        resp = await process_image(frame_path)
        if resp:
            await state.update_data(yandex_response=resp)
            await send_result_page(message, resp)
        else:
            await message.answer("❌ Не удалось обработать кадр из видео.")

    except Exception as e:
        logger.error(f"Error processing TikTok: {e}")
        await message.answer(f"Ошибка: {str(e)}")
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
        if frame_path and os.path.exists(frame_path):
            os.remove(frame_path)


@dp.message(Command("anime"))
async def cmd_anime_search(message: Message, state: FSMContext):
    """Поиск информации об аниме на Shikimori"""
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        await search_anime_info(message, args[1])
    else:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="❌ Отмена поиска",
            callback_data=CancelTypes.CANCEL_ANIME_SEARCH
        )
        await message.answer(
            "Введите название аниме для поиска:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(AnimeSearchStates.waiting_for_anime_name)


@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Главное меню:", reply_markup=actions_keyboard())

@dp.callback_query(F.data.startswith("cancel_type:"))
async def process_cancel(callback: CallbackQuery, state: FSMContext):
    try:
        cancel_type = callback.data.split(":")[1]

        if cancel_type == "anime_search":
            current_state = await state.get_state()
            if current_state == AnimeSearchStates.waiting_for_anime_name:
                await state.set_state(None)
                await callback.message.edit_text("🔍 Поиск аниме отменён")

        elif cancel_type == "no_send_admin":
            current_state = await state.get_state()
            if current_state == AdminStates.waiting_for_contact_message:
                await state.set_state(None)
                await callback.message.edit_text("✉️ Отправка админу отменена")

        await callback.answer()

    except (IndexError, ValueError):
        await callback.answer("Ошибка отмены", show_alert=True)
    except Exception as e:
        logger.error(f"Cancel error: {e}")
        await callback.answer("Произошла ошибка", show_alert=True)

@dp.message(AnimeSearchStates.waiting_for_anime_name)
async def process_anime_name(message: Message, state: FSMContext):
    # Очищаем только состояние ожидания названия аниме
    await state.set_data({**await state.get_data(), **{
        "state": None,  # Сбрасываем состояние
        "last_anime_search": message.text  # Можно сохранить последний поиск, если нужно
    }})
    await search_anime_info(message, message.text, state)


async def search_anime_info(message: Message, anime_name: str, state: FSMContext):
    """Ищет информацию об аниме на Shikimori"""
    await message.answer(f"🔍 Ищу информацию об аниме '{anime_name}'...")

    try:
        search_results = await parser.search(anime_name)
        if not search_results:
            await message.answer(f"❌ Аниме '{anime_name}' не найдено.")
            return

        anime_data = search_results[0]
        detailed_info = await parser.anime_info(anime_data['link'])

        message_parts = [
            f"🎬 <b>Название:</b> {anime_data['title']}",
            f"🔹 <b>Оригинальное название:</b> {anime_data['original_title']}",
        ]

        if detailed_info:
            if detailed_info.get('type'):
                message_parts.append(f"📺 <b>Тип:</b> {detailed_info['type']}")
            if detailed_info.get('dates'):
                message_parts.append(f"📅 <b>Даты выхода:</b> {detailed_info['dates']}")
            if detailed_info.get('status'):
                message_parts.append(f"🔄 <b>Статус:</b> {detailed_info['status']}")
            if detailed_info.get('episodes'):
                message_parts.append(f"🎞️ <b>Эпизоды:</b> {detailed_info['episodes']}")
            if detailed_info.get('episode_duration'):
                message_parts.append(f"⏱ <b>Длительность:</b> {detailed_info['episode_duration']}")
            if detailed_info.get('studio'):
                message_parts.append(f"🏢 <b>Студия:</b> {detailed_info['studio']}")
            if detailed_info.get('genres'):
                message_parts.append(f"🏷️ <b>Жанры:</b> {', '.join(detailed_info['genres'])}")
            if detailed_info.get('themes'):
                message_parts.append(f"🎭 <b>Темы:</b> {', '.join(detailed_info['themes'])}")
            if detailed_info.get('score'):
                message_parts.append(f"⭐ <b>Оценка:</b> {detailed_info['score']}")
            if detailed_info.get('rating'):
                message_parts.append(f"🔞 <b>Рейтинг:</b> {detailed_info['rating']}")

        message_parts.append(f"\n🔗 <a href='{anime_data['link']}'>Подробнее на Shikimori</a>")

        poster_url = detailed_info.get('picture') if detailed_info else anime_data.get('poster')
        if poster_url:
            try:
                await message.answer_photo(
                    photo=poster_url,
                    caption="\n".join(message_parts)
                )
            except Exception as e:
                logger.error(f"Не удалось отправить постер: {e}")
                await message.answer("\n".join(message_parts))
        else:
            await message.answer("\n".join(message_parts))

        if detailed_info and detailed_info.get('description'):
            description = detailed_info['description']
            if len(description) > 1000:
                description = description[:1000] + "..."
            await message.answer(f"📖 <b>Описание:</b>\n{description}")

    except Exception as e:
        logger.error(f"Error searching anime: {e}")
        await message.answer(f"Произошла ошибка при поиске аниме: {e}")
    finally:
        current_state = await state.get_state()
        if current_state == AnimeSearchStates.waiting_for_anime_name:
            await state.set_state(None)

        await message.answer(
            "❤ Понравился бот?\n\nПоделись им с другом или знакомым 🤗",
            reply_markup=share_bot()
        )



@dp.callback_query(F.data == "contact_admin")
async def contact_admin_callback(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="❌ Не отправлять",
        callback_data=CancelTypes.CANCEL_ADMIN_MESSAGE
    )
    await callback.message.answer(
        "Напишите ваше сообщение для админа (текст или картинка с текстом):",
        reply_markup=builder.as_markup()
    )
    await state.set_state(AdminStates.waiting_for_contact_message)
    await callback.answer()


@dp.message(AdminStates.waiting_for_contact_message)
async def process_contact_message(message: Message, state: FSMContext):
    admin_id = int(ADMIN_ID)

    text = f"Сообщение от пользователя <code>{message.from_user.id}</code> (@{message.from_user.username}):\n"

    if message.text:
        text += message.text
        text += "\n\nИспользуй /answer userid текст - для ответа"
        await bot.send_message(admin_id, text)
    elif message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        temp_image_path = f"temp/contact_{uuid.uuid4()}.jpg"
        await bot.download_file(file_path, temp_image_path)

        with open(temp_image_path, "rb") as photo_file:
            await bot.send_photo(
                admin_id,
                BufferedInputFile(photo_file.read(), filename="contact.jpg"),
                caption=text + (f"\n\n{message.caption}" if message.caption else "\n[без текста]") +
                        "\n\nИспользуй /answer userid текст - для ответа"
            )
        os.remove(temp_image_path)
    else:
        await message.answer("Пожалуйста, отправьте текст или фото с текстом одним сообщением")
        return

    await message.answer("Ваше сообщение отправлено админу!")
    await state.clear()


@dp.message(Command("answer"))
async def admin_answer(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        return

    try:
        _, user_id, *answer_text = message.text.split()
        answer_text = " ".join(answer_text)
        await message.bot.send_message(int(user_id), f"Ответ от админа:\n{answer_text}")
        await message.answer(f"Ответ отправлен пользователю {user_id}")
    except (ValueError, IndexError) as e:
        await message.answer(f"Ошибка: {e}\nИспользуйте: /answer user_id текст")
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("sendall"))
async def send_to_all_users(message: Message):
    """Отправляет сообщение всем пользователям из БД"""
    if str(message.from_user.id) != ADMIN_ID:
        return

    try:
        sendtext = " ".join(message.text.split()[1:])
        if not sendtext:
            await message.answer("Не указан текст сообщения!\nИспользуйте: /sendall текст")
            return

        await message.answer(f"Начинаю рассылку сообщения для всех пользователей...")

        async with await create_pool() as pool:
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT user_id FROM users")
                    users = await cursor.fetchall()

        total_users = len(users)
        success = 0
        failed = 0
        failed_ids = []

        for user in users:
            user_id = user[0]
            try:
                await asyncio.sleep(1)
                await bot.send_message(user_id, sendtext)
                success += 1
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                failed += 1
                failed_ids.append(str(user_id))
                if "bot was blocked" in str(e).lower():
                    async with await create_pool() as pool:
                        async with pool.acquire() as conn:
                            async with conn.cursor() as cursor:
                                await cursor.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
                                await conn.commit()

        report = (
            f"Рассылка завершена!\n"
            f"Всего пользователей: {total_users}\n"
            f"Успешно отправлено: {success}\n"
            f"Не удалось отправить: {failed}"
        )

        if failed_ids:
            report += f"\n\nНе удалось отправить для ID: {', '.join(failed_ids[:10])}"
            if len(failed_ids) > 10:
                report += f" и ещё {len(failed_ids) - 10}..."

        await message.answer(report)

    except Exception as e:
        await message.answer(f"Ошибка при рассылке: {str(e)}")


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    if not os.path.exists("temp"):
        os.makedirs("temp")

    asyncio.run(main())