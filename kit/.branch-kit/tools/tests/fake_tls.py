"""Сертификат для поддельной площадки по https — средствами стандартной
библиотеки: ключ RSA 2048 и самоподписанный сертификат на 127.0.0.1.
Ключ и сертификат пишутся во временный каталог проверки и живут, пока
жив он; в репозитории их нет."""

import base64
import datetime
import hashlib
import os
import secrets
import ssl
import textwrap

SMALL_PRIMES = [n for n in range(3, 2000, 2)
                if all(n % d for d in range(3, int(n ** 0.5) + 1, 2))]
E = 65537
# DigestInfo для SHA-256 перед хешем в подписи RSA по PKCS #1 v1.5.
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _probably_prime(n, rounds=32):
    if any(n % p == 0 for p in SMALL_PRIMES):
        return False
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    """Простое число ровно в bits разрядов с двумя старшими единицами:
    произведение двух таких — ровно 2·bits разрядов."""
    while True:
        n = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if n % E != 1 and _probably_prime(n):
            return n


# --- DER: только то, что нужно сертификату и ключу

def _tlv(tag, body):
    size = len(body)
    if size < 0x80:
        head = bytes([size])
    else:
        raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
        head = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + head + body


def _int(value):
    return _tlv(0x02, value.to_bytes(value.bit_length() // 8 + 1, "big"))


def _seq(*items):
    return _tlv(0x30, b"".join(items))


def _oid(dotted):
    parts = [int(x) for x in dotted.split(".")]
    body = bytes([40 * parts[0] + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, body)


def _bits(data):
    return _tlv(0x03, b"\x00" + data)


def _octets(data):
    return _tlv(0x04, data)


def _time(moment):
    return _tlv(0x17, moment.strftime("%y%m%d%H%M%SZ").encode("ascii"))


def _pem(kind, der):
    lines = textwrap.wrap(base64.b64encode(der).decode("ascii"), 64)
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (
        kind, "\n".join(lines), kind)


def make_certificate(directory, address="127.0.0.1"):
    """Ключ и сертификат на address в directory; пути (сертификат, ключ).
    Сертификат — на сутки до и после текущей минуты."""
    p, q = _prime(1024), _prime(1024)
    n, phi = p * q, (p - 1) * (q - 1)
    d = pow(E, -1, phi)
    key_bytes = (n.bit_length() + 7) // 8
    public = _seq(_int(n), _int(E))
    key_id = hashlib.sha1(public).digest()
    name = _seq(_tlv(0x31, _seq(_oid("2.5.4.3"),
                                _tlv(0x0C, address.encode("ascii")))))
    algorithm = _seq(_oid("1.2.840.113549.1.1.11"), b"\x05\x00")
    now = datetime.datetime.now(datetime.timezone.utc)
    day = datetime.timedelta(days=1)
    ip = bytes(int(x) for x in address.split("."))
    extensions = _seq(
        _seq(_oid("2.5.29.17"), _octets(_seq(_tlv(0x87, ip)))),
        _seq(_oid("2.5.29.14"), _octets(_octets(key_id))),
        _seq(_oid("2.5.29.35"), _octets(_seq(_tlv(0x80, key_id)))))
    tbs = _seq(
        _tlv(0xA0, _int(2)), _int(secrets.randbits(63) | 1), algorithm,
        name, _seq(_time(now - day), _time(now + day)), name,
        _seq(_seq(_oid("1.2.840.113549.1.1.1"), b"\x05\x00"),
             _bits(public)),
        _tlv(0xA3, extensions))
    digest = SHA256_PREFIX + hashlib.sha256(tbs).digest()
    padded = (b"\x00\x01" + b"\xff" * (key_bytes - len(digest) - 3)
              + b"\x00" + digest)
    signature = pow(int.from_bytes(padded, "big"), d, n).to_bytes(
        key_bytes, "big")
    certificate = _seq(tbs, algorithm, _bits(signature))
    private = _seq(_int(0), _int(n), _int(E), _int(d), _int(p), _int(q),
                   _int(d % (p - 1)), _int(d % (q - 1)), _int(pow(q, -1, p)))
    cert_path = os.path.join(directory, "platform-cert.pem")
    key_path = os.path.join(directory, "platform-key.pem")
    with open(cert_path, "w", encoding="ascii", newline="\n") as f:
        f.write(ssl.DER_cert_to_PEM_cert(certificate))
    with open(key_path, "w", encoding="ascii", newline="\n") as f:
        f.write(_pem("RSA PRIVATE KEY", private))
    return cert_path, key_path


def server_context(directory):
    """Контекст TLS поддельной площадки и путь к её сертификату. Контекст
    строится без create_default_context: файл SSLKEYLOGFILE он не
    открывает, и ключи в файле проверки могли бы появиться только от
    клиента."""
    cert_path, key_path = make_certificate(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, cert_path
