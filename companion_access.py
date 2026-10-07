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


class AccessGate:
    def __init__(self, verify=verify_receipt, clock=time.monotonic):
        self._verify = verify
        self._clock = clock
        self._lock = threading.Lock()
        self._grants = {}
        self._attempts = []

    def _prune(self, now):
        self._grants = {ticket: row for ticket, row in self._grants.items() if row[0] > now}
        self._attempts = [at for at in self._attempts if now - at < 60]

    def authorize(self, data):
        receipt = data.get('receipt') if isinstance(data, dict) else None
        account = data.get('userId') if isinstance(data, dict) else None
        if not isinstance(receipt, str) or len(receipt) > 1024:
            raise AccessError('Update and activate the official Eclipse plugin, then reconnect.')
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
            now = self._clock()
            self._prune(now)
            if len(self._grants) >= 16:
                raise AccessError('Too many active Eclipse connections.')
            ticket = secrets.token_urlsafe(32)
            self._grants[ticket] = (now + LIFETIME, fingerprint)
            return {'ticket': ticket, 'expiresIn': LIFETIME}

    def allowed(self, ticket):
        if not isinstance(ticket, str) or len(ticket) > 128:
            return False
        row = self._grants.get(ticket)
        return row is not None and row[0] > self._clock()

    def clear(self):
        with self._lock:
            self._grants.clear()
            self._attempts.clear()
