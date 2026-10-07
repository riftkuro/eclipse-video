import base64
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request

VERIFY_URL = 'https://eclipse-keys.riftex.workers.dev/verify'
LIFETIME = 1800
TICKET_KEY = bytes.fromhex('1b272f05e1e0fc6d43f8b86cf8f1e9c1a14a1fd7c7ef0304508e308735b5ba0b')
TICKET_SKEW = 86400

P = 2**255-19
Q = 2**252+27742317777372353535851937790883648493
D = -121665*pow(121666, P-2, P) % P
ROOT = pow(2, (P-1)//4, P)


class AccessError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise AccessError('Activation verification redirected unexpectedly.')


REASONS = {
    'not_activated': 'This activation was removed. Activate Eclipse again in Studio.',
    'key_locked_to_other_user': 'Eclipse activation belongs to another Roblox account.',
    'bad_request': 'The saved Eclipse activation is invalid. Activate Eclipse again in Studio.',
    'invalid_token': 'The saved Eclipse activation expired or is invalid. Activate Eclipse again in Studio.',
}


def verify_receipt(token, user_id):
    body = json.dumps({'token': token, 'userId': user_id}).encode()
    request = urllib.request.Request(VERIFY_URL, data=body, headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'User-Agent': 'Eclipse-Companion-Access/2'})
    for attempt in range(2):
        try:
            try:
                response = urllib.request.build_opener(NoRedirect()).open(request, timeout=12)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.code
                payload = response.read(16385)
            if len(payload) > 16384:
                raise AccessError('Activation server returned an oversized response. Retry later.')
            try:
                result = json.loads(payload)
            except (ValueError, UnicodeError):
                result = None
            if isinstance(result, dict) and result.get('ok') is True and status == 200:
                return
            reason = result.get('error') if isinstance(result, dict) else None
            if isinstance(reason, str) and reason in REASONS:
                raise AccessError(REASONS[reason])
            if status == 429:
                raise AccessError('Activation server is busy (HTTP 429). Wait a minute; Eclipse will retry.')
            if status >= 500:
                if attempt == 0:
                    time.sleep(.5)
                    continue
                raise AccessError(f'Activation server is temporarily unavailable (HTTP {status}). Eclipse will retry.')
            if status in (401, 403):
                raise AccessError(f'Activation request was rejected (HTTP {status}). Reopen the official plugin and check its activation; if it works there, report this code.')
            raise AccessError(f'Activation server returned an invalid response (HTTP {status}). Update the companion and retry.')
        except AccessError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as exc:
            import ssl
            import socket
            reason = getattr(exc, 'reason', exc)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise AccessError('Activation connection failed certificate verification. Check the Windows date and trusted certificates; HTTPS verification is required.') from None
            if attempt == 0:
                time.sleep(.5)
                continue
            if isinstance(reason, (TimeoutError, socket.timeout)):
                detail = 'timed out'
            elif isinstance(reason, socket.gaierror):
                detail = 'could not resolve the activation server (DNS)'
            else:
                detail = 'could not reach the activation server'
            raise AccessError(f'Eclipse Video {detail}. Check its firewall/proxy connection; Eclipse will retry.') from None


def _point_add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    first = (y1-x1)*(y2-x2) % P
    second = (y1+x1)*(y2+x2) % P
    third = t1*2*D*t2 % P
    fourth = z1*2*z2 % P
    e, f, g, h = second-first, fourth-third, fourth+third, second+first
    return (e*f % P, g*h % P, f*g % P, e*h % P)


def _point_mul(scalar, point):
    result = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _point_equal(a, b):
    return (a[0]*b[2]-b[0]*a[2]) % P == 0 and (a[1]*b[2]-b[1]*a[2]) % P == 0


def _decompress(data):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, 'little')
    sign = y >> 255
    y &= (1 << 255)-1
    if y >= P:
        return None
    x2 = (y*y-1)*pow(D*y*y+1, P-2, P) % P
    if x2 == 0:
        return None if sign else (0, y, 1, 0)
    x = pow(x2, (P+3)//8, P)
    if (x*x-x2) % P:
        x = x*ROOT % P
    if (x*x-x2) % P:
        return None
    if x & 1 != sign:
        x = P-x
    return (x, y, 1, x*y % P)


BASE = _decompress((4*pow(5, P-2, P) % P).to_bytes(32, 'little'))


def ed25519_verify(public, message, signature):
    if len(signature) != 64:
        return False
    a = _decompress(public)
    r = _decompress(signature[:32])
    s = int.from_bytes(signature[32:], 'little')
    if a is None or r is None or s >= Q:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32]+public+message).digest(), 'little') % Q
    return _point_equal(_point_mul(s, BASE), _point_add(r, _point_mul(h, a)))


def _unpad(text):
    return base64.urlsafe_b64decode(text+'='*(-len(text) % 4))


def verify_ticket(ticket, user_id, now=None):
    if not isinstance(ticket, str) or len(ticket) > 512:
        raise AccessError('Companion ticket is invalid.')
    parts = ticket.split('.')
    if len(parts) != 3 or parts[0] != 'v1':
        raise AccessError('Companion ticket is invalid.')
    try:
        payload = json.loads(_unpad(parts[1]))
        signature = _unpad(parts[2])
    except (ValueError, UnicodeError):
        raise AccessError('Companion ticket is invalid.') from None
    if not ed25519_verify(TICKET_KEY, (parts[0]+'.'+parts[1]).encode(), signature):
        raise AccessError('Companion ticket signature is invalid.')
    if not isinstance(payload, dict) or payload.get('v') != 1 or payload.get('u') != user_id:
        raise AccessError('Companion ticket belongs to another Roblox account.')
    expires = payload.get('e')
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        raise AccessError('Companion ticket is invalid.')
    if (time.time() if now is None else now) > expires+TICKET_SKEW:
        raise AccessError('Companion ticket expired.')


class AccessGate:
    def __init__(self, verify=verify_receipt, clock=time.monotonic):
        self._verify = verify
        self._clock = clock
        self._lock = threading.Lock()
        self._grants = {}
        self._attempts = []
        self.last_error = ''

    def _prune(self, now):
        self._grants = {ticket: row for ticket, row in self._grants.items() if row[0] > now}
        self._attempts = [at for at in self._attempts if now - at < 60]

    def authorize(self, data):
        try:
            result = self._authorize(data)
        except AccessError as error:
            self.last_error = str(error)
            raise
        self.last_error = ''
        return result

    def _grant(self, fingerprint, now):
        self._prune(now)
        if len(self._grants) >= 16:
            oldest = min(self._grants, key=lambda ticket: self._grants[ticket][0])
            del self._grants[oldest]
        ticket = secrets.token_urlsafe(32)
        self._grants[ticket] = (now + LIFETIME, fingerprint)
        return {'ticket': ticket, 'expiresIn': LIFETIME}

    def _authorize(self, data):
        account = data.get('userId') if isinstance(data, dict) else None
        signed = data.get('ticket') if isinstance(data, dict) else None
        ticket_error = None
        if isinstance(signed, str) and signed and not isinstance(account, bool) and isinstance(account, int) and 0 < account < 2**53:
            try:
                verify_ticket(signed, account)
                with self._lock:
                    return self._grant(hashlib.sha256(signed.encode()).digest(), self._clock())
            except AccessError as error:
                ticket_error = error
        receipt = data.get('receipt') if isinstance(data, dict) else None
        if not isinstance(receipt, str) or len(receipt) > 1024:
            raise ticket_error or AccessError('Update and activate the official Eclipse plugin, then reconnect.')
        match = re.fullmatch(r'([0-9a-fA-F]{1,512}):([0-9]{1,16}):[0-9]{1,16}', receipt)
        if not match or isinstance(account, bool) or not isinstance(account, int) or not 0 < account < 2**53 or account != int(match[2]):
            raise AccessError('Eclipse activation does not match this Roblox account.')
        fingerprint = hashlib.sha256(receipt.encode()).digest()
        with self._lock:
            now = self._clock()
            self._prune(now)
            for ticket, (expires, known) in self._grants.items():
                if secrets.compare_digest(known, fingerprint) and expires - now > 60:
                    return {'ticket': ticket, 'expiresIn': int(expires - now)}
            if len(self._attempts) >= 6:
                raise AccessError('Too many activation checks. Wait a minute and reconnect.')
            self._attempts.append(now)
            self._verify(match[1], account)
            return self._grant(fingerprint, self._clock())

    def allowed(self, ticket):
        if not isinstance(ticket, str) or len(ticket) > 128:
            return False
        row = self._grants.get(ticket)
        return row is not None and row[0] > self._clock()

    def clear(self):
        with self._lock:
            self._grants.clear()
            self._attempts.clear()
