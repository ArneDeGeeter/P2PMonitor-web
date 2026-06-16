import time
import pyotp


def get_current_code(totp_secret: str) -> str:
    return pyotp.TOTP(totp_secret).now()


def seconds_remaining() -> int:
    return 30 - (int(time.time()) % 30)
