"""Legacy panel API backed exclusively by the shared installation settings."""
from desktop import notifications

KEY = 'pushplus_token'


def status(backend):
    return notifications.status(backend)


def save(backend, token):
    return notifications.save(backend, token)


def token_for_notification(backend):
    return notifications.configured_token(backend)


def test_notification(backend, *, opener=None):
    return notifications.test_notification(backend, opener=opener)
