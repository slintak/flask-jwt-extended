import datetime
import json
import uuid
from functools import wraps

import jwt
from werkzeug.local import LocalProxy
from flask import request, jsonify, current_app
try:
    # see: http://flask.pocoo.org/docs/0.11/extensiondev/
    from flask import _app_ctx_stack as ctx_stack
except ImportError:
    from flask import _request_ctx_stack as ctx_stack

from flask_jwt_extended.config import ALGORITHM, REFRESH_EXPIRES, ACCESS_EXPIRES, \
    BLACKLIST_ENABLED, BLACKLIST_STORE, BLACKLIST_TOKEN_CHECKS
from flask_jwt_extended.exceptions import JWTEncodeError, JWTDecodeError, \
    InvalidHeaderError, NoAuthHeaderError, WrongTokenError, RevokedTokenError, \
    FreshTokenRequired

# Proxy for accessing the identity of the JWT in this context
jwt_identity = LocalProxy(lambda: _get_identity())

# Proxy for getting the dictionary of custom user claims in this JWT
jwt_user_claims = LocalProxy(lambda: _get_user_claims())


def _get_identity():
    """
    Returns the identity of the JWT in this context. If no JWT is present,
    None is returned.
    """
    return getattr(ctx_stack.top, 'jwt_identity', None)


def _get_user_claims():
    """
    Returns the dictionary of custom use claims in this JWT. If no custom user
    claims are present, an empty dict is returned
    """
    return getattr(ctx_stack.top, 'jwt_user_claims', {})


def _encode_access_token(identity, secret, algorithm, token_expire_delta,
                         fresh, user_claims):
    """
    Creates a new access token.

    :param identity: Some identifier of who this client is (most common would be a client id)
    :param secret: Secret key to encode the JWT with
    :param fresh: If this should be a 'fresh' token or not
    :param algorithm: Which algorithm to use for the toek
    :return: Encoded JWT
    """
    # Verify that all of our custom data we are encoding is what we expect
    if user_claims is None:
        user_claims = {}
    if not isinstance(user_claims, dict):
        raise JWTEncodeError('user_claims must be a dict')
    if not isinstance(fresh, bool):
        raise JWTEncodeError('fresh must be a bool')
    try:
        json.dumps(user_claims)
    except Exception as e:
        raise JWTEncodeError('Error json serializing user_claims: {}'.format(str(e)))

    # Create the jwt
    now = datetime.datetime.utcnow()
    uid = str(uuid.uuid4())
    token_data = {
        'exp': now + token_expire_delta,
        'iat': now,
        'nbf': now,
        'jti': uid,
        'identity': identity,
        'fresh': fresh,
        'type': 'access',
        'user_claims': user_claims,
    }
    encoded_token = jwt.encode(token_data, secret, algorithm).decode('utf-8')
    _store_token_if_blacklist_enabled(token_data)
    return encoded_token


def _encode_refresh_token(identity, secret, algorithm, token_expire_delta):
    """
    Creates a new refresh token, which can be used to create subsequent access
    tokens.

    :param identity: Some identifier used to identify the owner of this token
    :param secret: Secret key to encode the JWT with
    :param algorithm: Which algorithm to use for the toek
    :return: Encoded JWT
    """
    now = datetime.datetime.utcnow()
    uid = str(uuid.uuid4())
    token_data = {
        'exp': now + token_expire_delta,
        'iat': now,
        'nbf': now,
        'jti': uid,
        'identity': identity,
        'type': 'refresh',
    }
    encoded_token = jwt.encode(token_data, secret, algorithm).decode('utf-8')
    _store_token_if_blacklist_enabled(token_data)
    return encoded_token


def _decode_jwt(token, secret, algorithm):
    """
    Decodes an encoded JWT

    :param token: The encoded JWT string to decode
    :param secret: Secret key used to encode the JWT
    :param algorithm: Algorithm used to encode the JWT
    :return: Dictionary containing contents of the JWT
    """
    # ext, iat, and nbf are all verified by pyjwt. We just need to make sure
    # that the custom claims we put in the token are present
    data = jwt.decode(token, secret, algorithm=algorithm)
    if 'jti' not in data or not isinstance(data['jti'], str):
        raise JWTDecodeError("Missing or invalid claim: jti")
    if 'identity' not in data:
        raise JWTDecodeError("Missing claim: identity")
    if 'type' not in data or data['type'] not in ('refresh', 'access'):
        raise JWTDecodeError("Missing or invalid claim: type")
    if data['type'] == 'access':
        if 'fresh' not in data or not isinstance(data['fresh'], bool):
            raise JWTDecodeError("Missing or invalid claim: fresh")
        if 'user_claims' not in data or not isinstance(data['user_claims'], dict):
            raise JWTDecodeError("Missing or invalid claim: user_claims")
    return data


def _decode_jwt_from_request():
    # Verify we have the auth header
    auth_header = request.headers.get('Authorization', None)
    if not auth_header:
        raise NoAuthHeaderError("Missing Authorization Header")

    # Make sure the header is valid
    parts = auth_header.split()
    if parts[0] != 'Bearer':
        msg = "Badly formatted authorization header. Should be 'Bearer <JWT>'"
        raise InvalidHeaderError(msg)
    elif len(parts) != 2:
        msg = "Badly formatted authorization header. Should be 'Bearer <JWT>'"
        raise InvalidHeaderError(msg)

    token = parts[1]
    secret = _get_secret_key()
    return _decode_jwt(token, secret, 'HS256')


def _handle_callbacks_on_error(fn):
    """
    Helper decorator that will catch any exceptions we expect to encounter
    when dealing with a JWT, and call the appropriate callback function for
    handling that error. Callback functions can be set in using the *_loader
    methods in jwt_manager.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        m = current_app.jwt_manager

        try:
            return fn(*args, **kwargs)
        except NoAuthHeaderError:
            return m.unauthorized_callback()
        except jwt.ExpiredSignatureError:
            return m.expired_token_callback()
        except (InvalidHeaderError, jwt.InvalidTokenError, JWTDecodeError,
                WrongTokenError) as e:
            return m.invalid_token_callback(str(e))
        except RevokedTokenError:
            return m.blacklisted_token_callback()
        except FreshTokenRequired:
            return m.token_needs_refresh_callback()
    return wrapper


def _check_blacklist(jwt_data):
    if not _blacklist_enabled():
        return

    store = _get_blacklist_store()
    token_type = jwt_data['type']
    jti = jwt_data['jti']

    if token_type == 'access' and _blacklist_checks() == 'all':
        token_status = store[jti]
        if token_status != 'active':
            raise RevokedTokenError('{} has been revoked'.format)

    if token_type == 'refresh' and _blacklist_checks() in ('all', 'refresh'):
        token_status = store[jti]
        if token_status != 'active':
            raise RevokedTokenError('{} has been revoked'.format)


def jwt_required(fn):
    """
    If you decorate a vew with this, it will ensure that the requester has a valid
    JWT before calling the actual view. This does not check the freshness of the
    token.

    See also: fresh_jwt_required()

    :param fn: The view function to decorate
    """
    @_handle_callbacks_on_error
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Attempt to decode the token
        jwt_data = _decode_jwt_from_request()

        # Verify this is an access token
        if jwt_data['type'] != 'access':
            raise WrongTokenError('Only access tokens can access this endpoint')

        # See if the token has been revoked (based on blacklist options)
        _check_blacklist(jwt_data)

        # Save the jwt in the context so that it can be accessed later by
        # the various endpoints that is using this decorator
        ctx_stack.top.jwt_identity = jwt_data['identity']
        ctx_stack.top.jwt_user_claims = jwt_data['user_claims']
        return fn(*args, **kwargs)
    return wrapper


def fresh_jwt_required(fn):
    """
    If you decorate a vew with this, it will ensure that the requester has a valid
    JWT before calling the actual view.

    See also: jwt_required()

    :param fn: The view function to decorate
    """
    @_handle_callbacks_on_error
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Attempt to decode the token
        jwt_data = _decode_jwt_from_request()

        # Verify this is an access token
        if jwt_data['type'] != 'access':
            raise WrongTokenError('Only access tokens can access this endpoint')

        # See if the token has been revoked (based on blacklist options)
        _check_blacklist(jwt_data)

        # Check if the token is fresh
        if not jwt_data['fresh']:
            raise FreshTokenRequired('Fresh token required')

        # Save the jwt in the context so that it can be accessed later by
        # the various endpoints that is using this decorator
        ctx_stack.top.jwt_identity = jwt_data['identity']
        ctx_stack.top.jwt_user_claims = jwt_data['user_claims']
        return fn(*args, **kwargs)
    return wrapper


def authenticate(identity):
    # Token settings
    config = current_app.config
    access_expire_delta = config.get('JWT_ACCESS_TOKEN_EXPIRES', ACCESS_EXPIRES)
    refresh_expire_delta = config.get('JWT_REFRESH_TOKEN_EXPIRES', REFRESH_EXPIRES)
    algorithm = config.get('JWT_ALGORITHM', ALGORITHM)
    secret = _get_secret_key()
    user_claims = current_app.jwt_manager.user_claims_callback(identity)

    # Actually make the tokens
    access_token = _encode_access_token(identity, secret, algorithm, access_expire_delta,
                                        fresh=True, user_claims=user_claims)
    refresh_token = _encode_refresh_token(identity, secret, algorithm,
                                          refresh_expire_delta)
    ret = {
        'access_token': access_token,
        'refresh_token': refresh_token
    }
    return jsonify(ret), 200


@_handle_callbacks_on_error
def refresh():
    # Get the JWT
    jwt_data = _decode_jwt_from_request()

    # verify this is a refresh token
    if jwt_data['type'] != 'refresh':
        raise WrongTokenError('Only refresh tokens can access this endpoint')

    # If blacklisting is enabled, see if this token has been revoked
    _check_blacklist(jwt_data)

    # Create and return the new access token
    config = current_app.config
    access_expire_delta = config.get('JWT_ACCESS_TOKEN_EXPIRES', ACCESS_EXPIRES)
    algorithm = config.get('JWT_ALGORITHM', ALGORITHM)
    secret = _get_secret_key()
    user_claims = current_app.jwt_manager.user_claims_callback(jwt_data['identity'])
    identity = jwt_data['identity']
    access_token = _encode_access_token(identity, secret, algorithm, access_expire_delta,
                                        fresh=False, user_claims=user_claims)
    ret = {'access_token': access_token}
    return jsonify(ret), 200


def fresh_authenticate(identity):
    # Token options
    secret = _get_secret_key()
    config = current_app.config
    access_expire_delta = config.get('JWT_ACCESS_TOKEN_EXPIRES', ACCESS_EXPIRES)
    algorithm = config.get('JWT_ALGORITHM', ALGORITHM)
    user_claims = current_app.jwt_manager.user_claims_callback(identity)
    access_token = _encode_access_token(identity, secret, algorithm, access_expire_delta,
                                        fresh=True, user_claims=user_claims)
    ret = {'access_token': access_token}
    return jsonify(ret), 200


def _get_secret_key():
    key = current_app.config.get('SECRET_KEY', None)
    if not key:
        raise RuntimeError('flask SECRET_KEY must be set')
    return key


def _blacklist_enabled():
    return current_app.config.get('JWT_BLACKLIST', BLACKLIST_ENABLED)


def _get_blacklist_store():
    return current_app.config.get('JWT_BLACKLIST_STORE', BLACKLIST_STORE)


def _blacklist_checks():
    return current_app.config.get('JWT_BLACKLIST_TOKEN_CHECKS', BLACKLIST_TOKEN_CHECKS)


def _store_supports_ttl(store):
    return getattr(store, 'ttl_support', False)


def _store_token_if_blacklist_enabled(token):
    # If the blacklist isn't enabled, do nothing
    if not _blacklist_enabled() or _blacklist_checks() is None:
        return

    # If configured to only check refresh tokens and this isn't a refresh token, return
    if _blacklist_checks() == 'refresh' and token['type'] != 'refresh':
        return

    # TODO store data as json in the store (including jti, identity, and user claims)

    # Otherwise store the token in the blacklist (with current status of active)
    store = _get_blacklist_store()
    if _store_supports_ttl(store):
        config = current_app.config
        if token['type'] == 'access':
            expire_delta = config.get('JWT_ACCESS_TOKEN_EXPIRES', ACCESS_EXPIRES)
        else:
            expire_delta = config.get('JWT_REFRESH_TOKEN_EXPIRES', REFRESH_EXPIRES)

        ttl = expire_delta + datetime.timedelta(minutes=15)
        ttl_secs = ttl.total_seconds()
        store.put(key=token['jti'], value="active", ttl_secs=ttl_secs)
    else:
        store.put(key=token['jti'], value="active")