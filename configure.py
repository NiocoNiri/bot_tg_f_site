"""Create private credentials without putting the password in command history."""
import getpass
import json
import os
from pathlib import Path


def main():
    target = Path(__file__).with_name("config.json")
    if target.exists():
        if input("Настройки уже существуют. Заменить? [y/N] ").lower() != "y":
            return
    username = input("Логин [owner]: ").strip() or "owner"
    if ":" in username:
        raise SystemExit("Логин не должен содержать двоеточие")
    password = getpass.getpass("Пароль (минимум 12 символов): ")
    if len(password) < 12:
        raise SystemExit("Нужен пароль минимум из 12 символов")
    if password != getpass.getpass("Повторите пароль: "):
        raise SystemExit("Пароли не совпадают")
    settings = {"host": "127.0.0.1", "port": 8000, "require_auth": True,
                "username": username, "password": password}
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(settings, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if os.name != "nt":
        target.chmod(0o600)
    print("Настройки сохранены. Держите config.json в тайне.")


if __name__ == "__main__":
    main()
