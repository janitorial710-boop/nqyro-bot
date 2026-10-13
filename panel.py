"""Связка с панелью (Remnawave / Marzban). Сейчас заглушка: подставь вызовы API панели."""
import config

async def ensure_user(uid: int) -> str:
    # TODO: создать пользователя в панели, если его нет (лимит устройств 3)
    # и вернуть реальную ссылку-подписку из панели.
    return f"{config.SUB_BASE}/{uid}"

async def set_expiry(uid: int, until_ts: int) -> None:
    # TODO: обновить дату окончания подписки пользователя в панели.
    return None
