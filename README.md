# AnimeScreenshot

## Что из себя представляет проект
Бот Telegram, написанный на Aiogram 3. 

Позволяет искать аниме по скриншоту/ссылке на ролик TikTok, Shorts

Есть система хранения пользователей в БД, рассылка сообщений админом 

## Функционал      
- [x] Поиск по скриншоту, ссылке
- [x] Перелистывание результатов поиска прямо в боте
- [x] Получение информации о аниме
  
## Цель проекта
Упростить поиск аниме, избавив от переходов в тг каналы, ботов и не искать по коду

## Технологии, используемые в боте
- Яндекс Картинки
- Shikimori API
- MySQL
  
## Зависимости
> [!NOTE]
> Python 3.9+

Установите библиотеки
```
pip install aiogram aiomysql opencv-python PicImageSearch anime-parsers-ru[async] python-dotenv logging asyncio
```

> [!IMPORTANT]
> Не забудьте создать .env файл и внести переменные окружения: ADMIN_ID, ANIME_BOT, DB_HOST, DB_PASSWORD, DB_USER, DB_PORT
