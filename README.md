# Личный калькулятор фьючерсов

Python 3.11 или новее. Интерфейс, мобильное оформление и расчёты находятся в app.py.
Сервер — Waitress. Регистрации, базы данных и истории расчётов нет.
Котировки обновляет один фоновый поток: сбой Мосбиржи не блокирует страницу.
При сбое показываются сохранённые данные с предупреждением. После перезапуска
кеш пуст, поэтому сначала нужно дождаться успешного ответа Мосбиржи.

## Запуск на Windows

В PowerShell из этой папки:

```powershell
.\start.ps1
```

При первом запуске задайте логин и пароль. Откройте http://127.0.0.1:8000.
Браузер покажет стандартное окно входа. Этот запуск работает пока процесс запущен;
для VPS круглосуточную работу обеспечивает служба ниже.

## Постоянная работа на Linux VPS (Debian/Ubuntu с systemd)

Для личного использования начните с 1 vCPU, 1 ГБ RAM, 10–20 ГБ SSD.
Скопируйте папку в /opt/bot_tg_f. Следующие команды выполняет администратор VPS:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv
sudo useradd --system --user-group --home-dir /opt/bot_tg_f --shell /usr/sbin/nologin botfutures
sudo python3 -m venv /opt/bot_tg_f/.venv
sudo /opt/bot_tg_f/.venv/bin/python -m pip install -r /opt/bot_tg_f/requirements.txt
sudo /opt/bot_tg_f/.venv/bin/python /opt/bot_tg_f/configure.py
sudo chown root:botfutures /opt/bot_tg_f/config.json
sudo chmod 640 /opt/bot_tg_f/config.json
sudo chmod 755 /opt/bot_tg_f
sudo install -m 644 /opt/bot_tg_f/bot-tg-f.service /etc/systemd/system/bot-tg-f.service
sudo systemctl daemon-reload
sudo systemctl enable --now bot-tg-f
sudo systemctl status bot-tg-f
```

Команда useradd нужна только один раз. Файлы приложения должны быть доступны
для чтения пользователю botfutures. Служба работает без прав администратора,
запускается после перезагрузки и перезапускается через 5 секунд при завершении.
Если 5 запусков подряд за минуту не удались, исправьте ошибку и выполните
`sudo systemctl reset-failed bot-tg-f`, затем `sudo systemctl start bot-tg-f`.
Логи ошибок: `sudo journalctl -u bot-tg-f -n 50 --no-pager`.
После изменения кода: `sudo systemctl restart bot-tg-f`.

## Доступ только для вас

По умолчанию сервер слушает только 127.0.0.1. Порт 8000 не открывайте в интернет.
Для компьютера достаточно SSH-туннеля (замените user и VPS_IP):

```bash
ssh -N -L 8080:127.0.0.1:8000 user@VPS_IP
```

Пока туннель открыт, сайт доступен по http://127.0.0.1:8080 на вашем компьютере.
Соединение до VPS зашифровано SSH. Логин и пароль сайта берутся из config.json.

Для обычного доступа с телефона через браузер используйте домен и HTTPS:
установите Caddy по официальной инструкции https://caddyserver.com/docs/install,
направьте DNS домена на VPS, добавьте блок из Caddyfile.example в /etc/caddy/Caddyfile
и замените пример домена. Разрешите входящие TCP 80 и 443, затем выполните:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Открывайте https://ВАШ_ДОМЕН. Пароль проверяет приложение; /health доступен без
пароля и сообщает только работоспособность процесса. Не передавайте Basic-пароль
по открытому HTTP в интернете. Caddy обслуживает HTTPS, приложение остаётся
на 127.0.0.1:8000. Автоматическое получение сертификата требует доступного домена.

## Настройки и обслуживание

config.json содержит host, port, username, password, require_auth. Создавайте его
через configure.py. На VPS после смены пароля снова назначьте группу botfutures,
права 640 и перезапустите службу. Не выкладывайте config.json и не отправляйте его
другим людям. Для резервной копии достаточно исходников и защищённой копии настроек.

Переменные HOST, PORT, SITE_USERNAME, SITE_PASSWORD, REQUIRE_AUTH имеют приоритет
над config.json. Без пароля разрешён только локальный запуск; служба требует пароль
всегда. Не меняйте HOST на 0.0.0.0 при использовании HTTPS-прокси или туннеля.

Проверка процесса: `curl http://127.0.0.1:8000/health`. Ответ `{"status":"ok"}`
не гарантирует доступность Мосбиржи: её состояние видно в предупреждении на странице.
Журнал не содержит пароли и параметры расчётов. Доступность 24/7 зависит также
от VPS и сети. Обновляйте ОС и проверяйте обновления Waitress; текущая зависимость
зафиксирована в requirements.txt для воспроизводимого запуска.

Эти файлы готовят запуск, но VPS, DNS и HTTPS не настроены автоматически.
