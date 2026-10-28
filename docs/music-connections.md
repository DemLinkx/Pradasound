# Дополнительные музыкальные библиотеки

## Что доступно без ключей

SoundCloud: импорт публичных лайков по `https://soundcloud.com/имя/likes`.
Это чтение публичного профиля, а не вход в аккаунт. Первые 50 треков,
пагинация до 500, поиск среди загруженных, добавление в общую очередь.
Полные публичные потоки извлекаются экспериментально через yt-dlp.
Preview и защищённые потоки не выдаются за полные песни.

SoundCloud перешёл на HLS/AAC. Main использует два прежних audio-слота
для предзагрузки/кроссфейда. Safari может играть HLS напрямую; в остальных
поддерживаемых браузерах по требованию загружается hls.js. VPS передаёт
ограниченные потоки без файлов, преобразования формата и транскодирования.
Случайные ссылки дают доступ только к заранее проверенным CDN SoundCloud.

## Spotify

Зарегистрировать собственное приложение Spotify, добавить точный Redirect URI
`https://prada-sound.space/oauth/spotify`, задать в приватном серверном `.env`
`SPOTIFY_CLIENT_ID` и `SPOTIFY_CLIENT_SECRET`. Ключи в Git не помещать.
Development Mode требует Premium у владельца приложения и ограничивает
разрешённых пользователей; их нужно добавить в dashboard Spotify.

Вход: OAuth Authorization Code + PKCE + state + HttpOnly cookie.
Импорт: личные сохранённые треки, по 50, максимум 500. Токены остаются
на сервере; браузеру возвращается непрозрачный временный handle.
При явном согласии пользователя название/исполнитель используются для поиска
на YouTube. В очередь попадает найденный YouTube ID; исходная запись Spotify
не скачивается. Версия записи может отличаться. Без ключей вход отключён;
публичный импорт плейлистов Spotify не заявляется как работающий.

## SoundCloud OAuth

Зарегистрировать собственное приложение SoundCloud, добавить Redirect URI
`https://prada-sound.space/oauth/soundcloud`, установить `SOUNDCLOUD_CLIENT_ID`
и `SOUNDCLOUD_CLIENT_SECRET`. Доступ к регистрации может требовать Artist Pro.
OAuth2.1/PKCE, обновление одноразового refresh token под lock, отключение
приложения через `/disconnect`. Авторизованные аудиоссылки получаются через
официальный API и доступ автора; при отказе нет скрытой подмены публичным треком.

## VK Музыка

Обычный VK ID не подтверждает доступ к закрытому VK Audio API. Подписка
пользователя также не даёт автоматически разрешение стороннему приложению.
По умолчанию подключение выключено. Только при наличии подтверждённого доступа
включить `VK_AUDIO_ENABLED=true`; экспериментальный ввод токена проверяет
`audio.get`, затем получает прямые полные потоки через `audio.getById`.
Пароли, полная браузерная сессия и чужие Client ID не используются.
Капча/закрытый API/отсутствующий прямой поток возвращают ошибку. Авторизация
через VK ID и закрытые аудиопотоки не объявляются реализованными.

## Маршруты и эксплуатация

`MUSIC_PROXY_URLS` задаёт прокси Spotify/SoundCloud; если пуст, используются
`YOUTUBE_PROXY_URLS`. Извлечение и загрузка SoundCloud используют один маршрут.
Яндекс и VK отправляют запросы напрямую. Не задавать общий `HTTP_PROXY` процессу.
OAuth в браузере пользователя всё равно зависит от доступности самого провайдера
с его устройства: VPN сервера не меняет маршрут внешнего окна входа.

Все сессии, pending OAuth и ссылки на медиа временные, хранятся в памяти
единственного процесса. Перезапуск сбрасывает их вместе с комнатами.
Хост получает поток по ID элемента очереди и host_token. Гость может добавить
только импортированный трек своего подключения; handle не включается в WS-состояние.
Замена подключения обновляет только элементы того же владельца.

## REST/WS контракт

- `GET /api/music/providers`: реальные возможности/причины недоступности.
- `POST /api/music/{provider}/login`, `POST /api/music/{provider}/callback`: OAuth.
- `POST /api/music/soundcloud/public`: публичная ссылка `/имя/likes`.
- `GET /api/music/{provider}/tracks?cursor=…`, заголовок `X-Music-Session`.
- `POST /api/music/spotify/resolve`: handle и импортированный track_id → YouTube ID.
- `POST /api/music/{provider}/disconnect`, заголовок `X-Music-Session`.
- WS `add_track`: `sc_<ID>` / `vk_<OWNER>_<ID>` и `provider_session`.
- WS `set_music_session`: provider + provider_session для своих элементов.
- `GET /api/music/queue/{lobby_id}/{item_id}/stream`, заголовок `X-Host-Token`.

Библиотека возвращает `session`, `authenticated`, `tracks`, `next_cursor`,
`has_more`. `authenticated=false` означает публичный импорт. В `tracks`
только метаданные/состояние доступности, без пользовательских токенов и stream URL.

## Источники

- https://developers.soundcloud.com/docs/api/guide
- https://developers.soundcloud.com/blog/api-streaming-urls/
- https://developer.spotify.com/documentation/web-api/concepts/quota-modes
- https://developer.spotify.com/documentation/web-api/reference/get-users-saved-tracks
- https://github.com/VKCOM/vk-api-schema

Тесты с подменённым API проверяют протокол/права и не доказывают доступность
живых аккаунтов. Spotify OAuth, приватный SoundCloud и VK требуют отдельной
проверки с собственным разрешённым приложением/аккаунтом перед включением.
