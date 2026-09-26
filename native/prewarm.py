"""A bounded, authenticated warm-up through Darkbloom's existing local engine.

No coordinator request, provider restart, cache purge, endpoint configuration,
or credentials in output. The loopback endpoint must belong to this session.
"""

import json, os, pathlib, stat, urllib.error, urllib.parse, urllib.request


class WarmupError(Exception):
    def __init__(self, message, *, code='warmup-failed'):
        super().__init__(message)
        self.code = code


class WarmupDeferred(WarmupError):
    """Capacity changed; observe paid work before retrying a synthetic request."""

    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WarmupError(
            'Warm-up refused an unexpected endpoint redirect.', code='endpoint-unsafe'
        )


def local_request(home, raw, options):
    if '--local-endpoint' not in options:
        raise WarmupError(
            'Enable Darkbloom’s authenticated local endpoint on the Mac to allow pre-warming.',
            code='endpoint-configuration',
        )
    if '--no-auth' in options:
        raise WarmupError(
            'Pre-warming requires an authenticated local endpoint.', code='endpoint-configuration'
        )
    path = pathlib.Path(home) / '.darkbloom/local.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise WarmupError(
                    'The local endpoint discovery file must be private to your Mac account.',
                    code='endpoint-unsafe',
                )
            d = json.loads(f.read(16385))
        url = urllib.parse.urlsplit(d['base_url'])
        port = int(options[options.index('--port') + 1]) if '--port' in options else 8000
        host = options[options.index('--bind') + 1] if '--bind' in options else '127.0.0.1'
        if (
            url.scheme != 'http'
            or url.hostname not in ('127.0.0.1', '::1')
            or host not in ('127.0.0.1', '::1', 'localhost')
            or url.port != port
            or d.get('port') != port
            or url.path != '/v1'
            or url.query
            or url.fragment
            or url.username
            or url.password
            or not isinstance(raw.get('pid'), int)
            or raw['pid'] <= 0
            or d.get('pid') != raw['pid']
        ):
            raise WarmupError(
                'Waiting for the current provider’s authenticated loopback endpoint.',
                code='endpoint-discovery',
            )
        token = d.get('api_key')
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 4096
            or '\r' in token
            or '\n' in token
        ):
            raise WarmupError(
                'The local endpoint authentication is unavailable.', code='endpoint-authentication'
            )
        return url.geturl() + '/chat/completions', token
    except WarmupError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        raise WarmupError(
            'Waiting for Darkbloom’s local endpoint discovery file.', code='endpoint-discovery'
        ) from None


def prewarm(home, raw, options, model, timeout=180, opener=None):
    url, token = local_request(home, raw, options)
    body = json.dumps(
        {
            'model': model,
            'messages': [{'role': 'user', 'content': 'Hi'}],
            'max_tokens': 1,
            'temperature': 0,
            'stream': False,
        }
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
    )
    client = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with client.open(request, timeout=timeout) as response:
            body = response.read(65537)
            if len(body) > 65536:
                raise WarmupError(
                    'Warm-up returned an unexpected response.', code='warmup-response'
                )
            value = json.loads(body)
        if not isinstance(value, dict):
            raise WarmupError('Warm-up returned an unexpected response.', code='warmup-response')
        # Thinking models may emit a reasoning token rather than visible text.
        usage = value.get('usage') or {}
        if not isinstance(usage, dict):
            raise WarmupError('Warm-up returned an unexpected response.', code='warmup-response')
        tokens = usage.get('completion_tokens')
        if (
            value.get('model') != model
            or not value.get('choices')
            or not isinstance(tokens, int)
            or isinstance(tokens, bool)
            or tokens < 1
        ):
            raise WarmupError(
                'The model did not confirm a completed warm-up token.', code='warmup-response'
            )
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            raise WarmupDeferred(
                'Darkbloom temporarily could not admit the warm-up. Waiting for capacity or verified serving output.',
                code='warmup-capacity',
            ) from None
        raise WarmupError(
            'The local model warm-up was rejected. Check Darkbloom on the Mac.',
            code='endpoint-authentication' if e.code in (401, 403) else 'warmup-rejected',
        ) from None
    except WarmupError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise WarmupError(
            'The local model warm-up did not finish. Check Darkbloom and available memory on the Mac.',
            code='warmup-transport',
        ) from None
