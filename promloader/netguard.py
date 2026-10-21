"""Скачивание по ссылке, которую дал кто-то чужой (фото из прайса поставщика): только публичный интернет.

Ссылка в прайсе — чужие данные. Без проверки «фото» вида http://127.0.0.1:8000/api/... или http://192.168.1.1/
заставило бы программу сходить в саму себя или в домашнюю сеть (SSRF), а огромный ответ — забить память.
"""

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

MAX_REDIRECTS = 5


class BlockedURL(ValueError):
    pass


def _bad_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast
            or addr.is_unspecified)


def check_url(url: str) -> None:
    """BlockedURL, если ссылка ведёт не в публичный интернет (или это не http/https)."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL("ссылка не http/https")
    host = parts.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise BlockedURL("ссылка на этот компьютер или домашнюю сеть")
    try:
        ipaddress.ip_address(host.split("%")[0])
        addresses = [host]
    except ValueError:
        try:
            addresses = [info[4][0] for info in socket.getaddrinfo(host, parts.port or 80, type=socket.SOCK_STREAM)]
        except (socket.gaierror, UnicodeError):
            return  # имя не находится — запрос всё равно не дойдёт; ошибку покажет само скачивание
    if any(_bad_ip(a) for a in addresses):
        raise BlockedURL("ссылка на этот компьютер или домашнюю сеть")


def fetch(url: str, max_bytes: int, timeout: float, transport=None) -> bytes:
    """Скачать не больше max_bytes. Каждое перенаправление проверяется заново (публичная ссылка могла бы
    перенаправить на 127.0.0.1). Превышение размера — ValueError, прочитав не больше max_bytes + кусок."""
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url)
            with client.stream("GET", url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                if r.status_code >= 400:
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                declared = r.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise ValueError("слишком большой файл")
                content = bytearray()
                for chunk in r.iter_bytes():
                    content += chunk
                    if len(content) > max_bytes:
                        raise ValueError("слишком большой файл")
                return bytes(content)
    raise BlockedURL("слишком много перенаправлений")
