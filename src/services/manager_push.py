"""Explicit manager consent, account-bound endpoint ownership and safe transport."""
import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from urllib.parse import urlsplit

from src.services.purchase_observation import _now

DEVICE_COOKIE = "gpcta_manager_push_device"

_PERMANENT_400_REASONS = (
    ('unauthorizedregistration', 'push_subscription_unauthorized'),
    ('invalidregistration', 'push_subscription_invalid'),
    ('invalidsubscription', 'push_subscription_invalid'),
    ('expiredsubscription', 'push_subscription_expired'),
    ('subscriptionnotfound', 'push_subscription_missing'),
)


def endpoint_lock(db):
    if db.is_postgres:
        db.execute("SET LOCAL lock_timeout = '1000ms'")
        db.execute('SELECT pg_advisory_xact_lock(1196442452,3)')
    elif not db.conn.in_transaction:
        db.execute('BEGIN IMMEDIATE')


def revoke(db, *, user_id=None, binding=None, endpoint=None):
    clauses, params = ['active=1'], []
    if user_id is not None:
        clauses.append('user_id=?'); params.append(user_id)
    if binding:
        clauses.append('binding_hash=?'); params.append(hashlib.sha256(binding.encode()).hexdigest())
    if endpoint:
        clauses.append('endpoint=?'); params.append(endpoint)
    if len(clauses) == 1:
        raise ValueError('Revocation scope required')
    db.execute('UPDATE manager_push_subscriptions SET active=0,revoked_at=?,updated_at=? WHERE '
               + ' AND '.join(clauses), (_now(), _now(), *params))


def revoke_binding(db, user_id, binding):
    """A tombstone also blocks an enrollment already authenticated before logout."""
    endpoint_lock(db)
    digest=hashlib.sha256(binding.encode()).hexdigest()
    db.execute('INSERT INTO manager_push_revocations(binding_hash,user_id,revoked_at) VALUES(?,?,?) ON CONFLICT(binding_hash) DO NOTHING',
               (digest,user_id,_now()))
    revoke(db,user_id=user_id,binding=binding)


def revoke_device(db, device, user_id=None):
    """The private device cookie survives auth expiry; no other device is revoked."""
    endpoint_lock(db)
    digest=hashlib.sha256(device.encode()).hexdigest()
    db.execute('INSERT INTO manager_push_revocations(binding_hash,user_id,revoked_at) VALUES(?,?,?) ON CONFLICT(binding_hash) DO NOTHING',
               ('device:'+digest,user_id,_now()))
    rows=db.execute('SELECT user_id,binding_hash FROM manager_push_subscriptions WHERE device_hash=? AND active=1',(digest,)).fetchall()
    for row in rows:
        db.execute('INSERT INTO manager_push_revocations(binding_hash,user_id,revoked_at) VALUES(?,?,?) ON CONFLICT(binding_hash) DO NOTHING',
                   (row['binding_hash'],row['user_id'],_now()))
    db.execute('UPDATE manager_push_subscriptions SET active=0,revoked_at=?,updated_at=? WHERE device_hash=? AND active=1',(_now(),_now(),digest))


def validate_subscription(data):
    if not isinstance(data, dict) or not isinstance(data.get('keys'), dict):
        raise ValueError('Assinatura inválida')
    endpoint = data.get('endpoint')
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValueError('Endpoint inválido')
    url = urlsplit(endpoint)
    host = (url.hostname or '').lower()
    trusted = host in ('fcm.googleapis.com','updates.push.services.mozilla.com','web.push.apple.com') or host.endswith('.notify.windows.com')
    if url.scheme != 'https' or not trusted or url.username or url.password or url.port not in (None,443) or url.fragment:
        raise ValueError('Serviço push não autorizado')
    keys = data['keys']
    for field, size in (('p256dh',65),('auth',16)):
        value = keys.get(field)
        if not isinstance(value,str) or len(value)>256:
            raise ValueError('Chaves inválidas')
        try:
            decoded = base64.b64decode(value + '=' * (-len(value)%4),altchars=b'-_',validate=True)
        except Exception:
            raise ValueError('Chaves inválidas') from None
        if len(decoded) != size or (field == 'p256dh' and decoded[0] != 4):
            raise ValueError('Chaves inválidas')
        if field == 'p256dh':
            from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey, SECP256R1
            try:
                EllipticCurvePublicKey.from_encoded_point(SECP256R1(),decoded)
            except ValueError:
                raise ValueError('Chave pública inválida') from None
    return endpoint, keys['p256dh'], keys['auth']


def register(db, user_id, binding, data, *, device=None):
    endpoint, p256dh, auth = validate_subscription(data)
    endpoint_lock(db)
    user = db.execute('SELECT role,active FROM users WHERE id=?'+(' FOR SHARE' if db.is_postgres else ''),(user_id,)).fetchone()
    if not user or user['role'] != 'manager' or not user['active']:
        raise PermissionError('Gestor não autorizado')
    digest = hashlib.sha256(binding.encode()).hexdigest()
    device_digest=hashlib.sha256((device or binding).encode()).hexdigest()
    if db.execute('SELECT 1 FROM manager_push_revocations WHERE binding_hash IN (?,?)',(digest,'device:'+device_digest)).fetchone():
        raise PermissionError('Associação de sessão revogada')
    # One browser endpoint has one account owner, including the legacy client path.
    db.execute('DELETE FROM push_subscriptions WHERE endpoint=?',(endpoint,))
    db.execute('INSERT INTO manager_push_subscriptions(user_id,endpoint,p256dh,auth,version,binding_hash,device_hash,consented_at,updated_at) '
               'VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,p256dh=excluded.p256dh,'
               'auth=excluded.auth,version=excluded.version,binding_hash=excluded.binding_hash,device_hash=excluded.device_hash,active=1,revoked_at=NULL,'
               'consented_at=excluded.consented_at,updated_at=excluded.updated_at',
               (user_id,endpoint,p256dh,auth,uuid.uuid4().hex,digest,device_digest,_now(),_now()))


def vapid_configuration_error(public_key=None, private_key=None):
    """Validate the configured VAPID pair without returning or logging key material."""
    public_key = (public_key if public_key is not None else os.environ.get('VAPID_PUBLIC_KEY') or '').strip()
    private_key = (private_key if private_key is not None else os.environ.get('VAPID_PRIVATE_KEY') or '').strip()
    if not public_key or not private_key:
        return 'vapid_unavailable'
    try:
        public_bytes = base64.b64decode(public_key + '=' * (-len(public_key) % 4), altchars=b'-_', validate=True)
        private_bytes = base64.b64decode(private_key + '=' * (-len(private_key) % 4), altchars=b'-_', validate=True)
        if len(public_bytes) != 65 or public_bytes[0] != 4:
            return 'vapid_invalid'
        from cryptography.hazmat.primitives.asymmetric.ec import derive_private_key, EllipticCurvePrivateKey, SECP256R1
        from cryptography.hazmat.primitives.serialization import load_der_private_key
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        private = (derive_private_key(int.from_bytes(private_bytes, 'big'), SECP256R1())
                   if len(private_bytes) == 32 else load_der_private_key(private_bytes, password=None))
        if not isinstance(private, EllipticCurvePrivateKey) or not isinstance(private.curve, SECP256R1):
            return 'vapid_invalid'
        derived = private.public_key()
        derived_bytes = derived.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    except (ValueError, TypeError):
        return 'vapid_invalid'
    if not hmac.compare_digest(public_bytes, derived_bytes):
        return 'vapid_key_mismatch'
    return None


def ready():
    return vapid_configuration_error() is None


def error_details(exc):
    """Return a bounded diagnostic code and whether the current subscription is unusable."""
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    if status in (404, 410):
        return 'push_subscription_gone', True
    if type(exc).__name__ != 'WebPushException':
        return type(exc).__name__[:100], False
    if status != 400:
        return ('webpush_http_' + str(status)) if status else 'webpush_transport_error', False
    body = ''
    try:
        body = response.text or ''
    except Exception:
        pass
    normalized = re.sub(r'[^a-z0-9]', '', str(body).lower())[:4096]
    for marker, code in _PERMANENT_400_REASONS:
        if marker in normalized:
            return code, True
    return 'webpush_http_400', False


def send(subscription, alert_id):
    from pywebpush import webpush
    # Generic lock-screen content; the destination independently checks manager access.
    payload = dict(title='Compras Inteligentes',body='Há uma nova necessidade de reposição. Consulte o planejamento.',
                   url='/stock/purchase-monitoring',alert_id=str(alert_id),kind='purchase-alert')
    return webpush(subscription_info={'endpoint':subscription['endpoint'],'keys':{'p256dh':subscription['p256dh'],'auth':subscription['auth']}},
                   data=json.dumps(payload),vapid_private_key=os.environ['VAPID_PRIVATE_KEY'],
                   vapid_claims={'sub':os.environ.get('VAPID_SUBJECT','mailto:diretoriagpcta@gmail.com')},
                   timeout=2,ttl=900,headers={'Topic':'gpcta-stock-' + str(alert_id)})
