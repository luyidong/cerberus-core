# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2016, OVH SAS
#
# This file is part of Cerberus-core.
#
# Cerberus-core is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


"""
    Utils for worker
"""

import base64
import hashlib
import json
import os
import re
import socket

from datetime import datetime
from functools import wraps
from time import sleep
from urlparse import urlparse

import chardet
import html2text
import netaddr
import requests
from cryptography.fernet import Fernet, InvalidSignature, InvalidToken
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.db.models import ObjectDoesNotExist
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_ipv46_address
from django.template import (Context, TemplateEncodingError,
                             TemplateSyntaxError, loader)
from redis import ConnectionError as RedisError
from redis import Redis
from requests.exceptions import (ChunkedEncodingError, ConnectionError,
                                 HTTPError, Timeout)
from rq import Queue
from rq_scheduler import Scheduler
from simplejson import JSONDecodeError

from abuse.models import MailTemplate, Role, User
from adapters.services.mailer.abstract import Email
from logger import get_logger

Logger = get_logger(os.path.basename(__file__))

CHARSETS = ('iso-8859-1', 'iso-8859-15', 'ascii', 'utf-16', 'windows-1252', 'cp850', 'iso-8859-11')
CERBERUS_USERS = User.objects.all().values_list('username', flat=True)

IPS_NETWORKS = {}
BLACKLISTED_NETWORKS = []

default_queue = Queue(
    connection=Redis(**settings.REDIS),
    **settings.QUEUE['default']
)

email_queue = Queue(
    connection=Redis(**settings.REDIS),
    **settings.QUEUE['email']
)

kpi_queue = Queue(
    connection=Redis(**settings.REDIS),
    **settings.QUEUE['kpi']
)

scheduler = Scheduler(connection=Redis(**settings.REDIS))
redis = Redis(**settings.REDIS)

html2text.ignore_images = True
html2text.images_to_alt = True
html2text.ignore_links = True

DNS_ERROR = {
    '-2': 'NXDOMAIN'
}


class EmailThreadTemplateNotFound(Exception):
    """
        EmailThreadTemplateNotFound
    """
    def __init__(self, message):
        super(EmailThreadTemplateNotFound, self).__init__(message)


class EmailThreadTemplateSyntaxError(Exception):
    """
        EmailThreadTemplateSyntaxError
    """
    def __init__(self, message):
        super(EmailThreadTemplateSyntaxError, self).__init__(message)


class CryptoException(Exception):
    """
        CryptoException
    """
    def __init__(self, message):
        super(CryptoException, self).__init__(message)


class Crypto(object):
    """
        Symmetric crypto for token
    """
    def __init__(self):

        self._salt = settings.SECRET_KEY
        self._kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self._salt,
            iterations=100000,
            backend=default_backend()
        )
        self._key = base64.urlsafe_b64encode(self._kdf.derive(settings.SECRET_KEY))
        self._fernet = Fernet(self._key)

    def encrypt(self, data):
        """
            Symmetric encryption using django's secret key
        """
        try:
            encrypted = self._fernet.encrypt(data)
            return encrypted
        except (InvalidSignature, InvalidToken):
            raise CryptoException('unable to encrypt data')

    def decrypt(self, data):
        """
            Symmetric decryption using django's secret key
        """
        try:
            encrypted = self._fernet.decrypt(data)
            return encrypted
        except (InvalidSignature, InvalidToken):
            raise CryptoException('unable to decrypt data')


class RoleCache(object):
    """
        Class caching allowed API routes for each `abuse.models.Role`
    """
    def __init__(self):
        self.routes = {}
        self._populate()

    def reset(self):
        """
            Reset the cache
        """
        self._clear()
        self._populate()

    def is_valid(self, role, method, endpoint):
        """
            Check if tuple (method, endpoint) for given role exists

            :param str role: The `abuse.models.Role` codename
            :param str method: The HTTP method
            :param str endpoint: The API endpoint
            :rtype: bool
            :return: if allowed or not
        """
        return (method, endpoint) in self.routes[role]

    def _clear(self):

        self.routes = {}

    def _populate(self):

        for role in Role.objects.all():
            self.routes[role.codename] = []
            for method, endpoint in role.allowedRoutes.all().values_list('method', 'endpoint'):
                self.routes[role.codename].append((method, endpoint))


class RequestException(Exception):
    """
        RequestException
    """
    def __init__(self, message, code):
        super(RequestException, self).__init__(message)
        self.code = code


def request_wrapper(url, auth=None, params=None, as_json=False,
                    method='POST', headers=None, timeout=30):
    """
        Python-requests wrapper
    """
    request = None
    func_params = {
        'headers': headers,
        'auth': auth,
        'params': params,
        'data': params,
        'verify': True,
        'timeout': timeout,
    }

    max_tries = 3  # Because sometimes network or backend is instable (TCP RST, HTTP 500 etc ...)
    for retry in xrange(max_tries):
        try:
            if method == 'GET':
                func_params.pop('data', None)
            else:
                func_params.pop('params', None)

            request = getattr(requests, method.lower())(url, **func_params)
            request.raise_for_status()
            request.connection.close()
            if as_json:
                return request.json()
            return request
        except HTTPError as ex:
            if 500 <= int(ex.response.status_code) <= 599:
                if retry == max_tries - 1:
                    raise RequestException(
                        _get_request_exception_message(request, url, params, ex),
                        ex.response.status_code
                    )
                else:
                    sleep(1)
            else:
                raise RequestException(
                    _get_request_exception_message(request, url, params, ex),
                    ex.response.status_code
                )
        except Timeout as ex:
            raise RequestException(_get_request_exception_message(request, url, params, ex), None)
        except (ChunkedEncodingError, ConnectionError, JSONDecodeError) as ex:
            if retry == max_tries - 1:
                raise RequestException(
                    _get_request_exception_message(request, url, params, ex),
                    None
                )
            else:
                sleep(1)


def _get_request_exception_message(request, url, params, exception):
    """
        Try to extract message from requests exeption
    """
    try:
        data = request.json()
        message = data['message']
    except (AttributeError, KeyError, JSONDecodeError, NameError, TypeError):
        message = str(exception)

    Logger.warning(unicode('error while fetching url %s, %s : %s' % (url, params, message)))
    return message


def get_url_hostname(url):
    """
        Try to get domain for an url

        :param str url: The url to extract hostname
        :rtype: str
        :return: the hostname or None
    """
    try:
        validate = URLValidator(schemes=('http', 'https', 'ftp', 'ftps', 'rtsp', 'rtmp'))
        validate(url)
    except (ValueError, ValidationError):
        return None

    parsed = urlparse(url)
    return parsed.hostname


def get_ips_from_url(url):
    """
        Retrieve IPs from url

        :param str url: The url to resolve
        :rtype: list
        :return: the list of resolved IP address for given url
    """
    try:
        parsed = urlparse(url)
        if parsed.hostname:
            socket.setdefaulttimeout(5)
            ips = socket.gethostbyname_ex(parsed.hostname)[2]
            return ips
    except (ValueError, socket.error, socket.gaierror, socket.herror, socket.timeout):
        pass


def get_ips_from_fqdn(fqdn):
    """
        Retrieve IPs from FQDN

        :param str fqdn: The FQDN to resolve
        :rtype: list
        :return: the list of resolved IP address for given FQDN
    """
    try:
        socket.setdefaulttimeout(5)
        ips = socket.gethostbyname_ex(fqdn)[2]
        return ips
    except (ValueError, socket.error, socket.gaierror, socket.herror, socket.timeout):
        return None


def get_reverses_for_item(item, nature='IP', replace_exception=False):
    """
        Try to get reverses infos for given item

        :param str item: Can be an IP address, a URL or a FQDN
        :param str nature: The nature of the item
        :param bool replace_exception: Replace socket error by NXDOMAIN or TIMEOUT
        :rtype: dict
        :return: a dict containing reverse infos
    """
    hostname = None
    reverses = {}

    if nature == 'IP':
        reverses['ip'] = item
        try:
            validate_ipv46_address(item)
            reverses['ipReverse'] = socket.gethostbyaddr(item)[0]
            reverses['ipReverseResolved'] = socket.gethostbyname(reverses['ipReverse'])
        except (IndexError, socket.error, socket.gaierror, socket.herror,
                socket.timeout, TypeError, ValidationError):
            pass
    elif nature == 'URL':
        reverses['url'] = item
        parsed = urlparse(item)
        if parsed.hostname:
            hostname = parsed.hostname
    else:
        reverses['fqdn'] = item
        hostname = item

    if hostname:
        try:
            reverses['fqdn'] = hostname
            reverses['fqdnResolved'] = socket.gethostbyname(hostname)
            reverses['fqdnResolvedReverse'] = socket.gethostbyaddr(reverses['fqdnResolved'])[0]
        except socket.gaierror as ex:
            if replace_exception:
                try:
                    reverses['fqdnResolved'] = DNS_ERROR[str(ex.args[0])]
                except KeyError:
                    reverses['fqdnResolved'] = 'NXDOMAIN'
        except socket.timeout:
            if replace_exception:
                reverses['fqdnResolved'] = 'TIMEOUT'
        except (IndexError, TypeError, socket.error, socket.herror):
            pass

    return reverses


def push_notification(data, user=None):
    """
        Push notification to Cerberus user(s)

        :param dict data: The content of the notification
    """
    if not user:
        notif_queues = ['cerberus:notification:%s' % (username) for username in CERBERUS_USERS]
    else:
        notif_queues = ['cerberus:notification:%s' % (user.username)]

    for notif_queue in notif_queues:
        try:
            redis.rpush(
                notif_queue,
                json.dumps(data),
            )
        except RedisError:
            pass


def get_user_notifications(username, limit=3):
    """
        Get notifications for given user

        :param str username: The username of the user
        :param int limit: The number of notifications to return
        :rtype: list
        :return: A list of dict
    """
    notification_queue = 'cerberus:notification:%s' % (username)
    response = []

    if not limit:
        return response

    for _ in xrange(0, limit):
        if redis.llen(notification_queue) == 0:
            break
        notification = redis.blpop(notification_queue)[1]
        response.append(json.loads(notification))

    return response


def dehtmlify(body):
    """
        Try to dehtmlify a text

        :param str body: The html content
        :rtype: str
        :return: The dehtmlified content
    """
    html = html2text.HTML2Text()
    html.body_width = 0
    body = html.handle(body.replace('\r\n', '<br/>'))
    body = re.sub(r'^(\s*\n){2,}', '\n', body, flags=re.MULTILINE)
    return body


def decode_every_charset_in_the_world(content, supposed_charset=None):
    """
        Try to detect encoding.
        If already in unicode, no need to go further (btw, a caught exception is raised.)

        :param str content: The content to decode
        :param str supposed_charset: A supposed encoding for given content
        :rtype: str
        :return: The decoded content
    """
    try:
        guessed_charset = chardet.detect(content)['encoding']
    except ValueError:
        return content

    if supposed_charset:
        charsets = ['utf-8', supposed_charset, guessed_charset] + list(CHARSETS)
    else:
        charsets = ['utf-8', guessed_charset] + list(CHARSETS)

    charsets = sorted(set(charsets), key=charsets.index)

    for chset in charsets:
        try:
            return content.decode(chset)
        except (LookupError, UnicodeError, UnicodeDecodeError, TypeError):
            continue


def get_ip_network(ip_str):
    """
        Try to return the owner of the IP address (based on ips.py)

        :param str ip_str: The IP address
        :rtype: str
        :return: The owner if find else None
    """
    try:
        ip_addr = netaddr.IPAddress(ip_str)
    except (netaddr.AddrConversionError, netaddr.AddrFormatError):
        return None

    for brand, networks in IPS_NETWORKS.iteritems():
        for net in networks:
            if net.netmask.value & ip_addr.value == net.value:
                return brand
    return None


def is_ipaddr_ignored(ip_str):
    """
        Check if the `ip_addr` is blacklisted

        :param str ip_str: The IP address
        :rtype: bool
        :return: If the ip_addr has to be ignored
    """
    ip_addr = netaddr.IPAddress(ip_str)

    for network in BLACKLISTED_NETWORKS:
        if network.netmask.value & ip_addr.value == network.value:
            return True
    return False


def is_valid_ipaddr(ip_addr):
    """
        Check if the `ip_addr` is a valid ipv4

        :param str ip_str: The IP address
        :rtype: bool
        :return: If the ip_addr is valid
    """
    try:
        validate_ipv46_address(ip_addr)
        return True
    except ValidationError:
        return False


def string_to_underscore_case(string):
    """
        Convert a string to underscore case

        :param str string: The sting to convert
        :rtype: str
        :return: The converted string
    """
    tmp = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', string)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', tmp).lower()


def get_attachment_storage_filename(hash_string=None, content=None, filename=None):
    """
        Generate a pseudo-unique filename based on content and filename

        :param str hash_string: a hash if it has been previously computed
        :param str content: the content of the file
        :param str filename: the real name of the file
    """
    storage_filename = None

    if content:
        hash_string = hashlib.sha256(content).hexdigest()

    storage_filename = hash_string + '-attach-'
    storage_filename = storage_filename.encode('utf-8')
    storage_filename = storage_filename + filename
    return storage_filename


def get_email_thread_content(ticket, emails):
    """
        Generate `abuse.models.Ticket` emails thred history
        based on 'email_thread' `abuse.models.MailTemplate`

        :param `abuse.models.Ticket` ticket: The cererus ticket
        :param list emails: a list of `adapters.services.mailer.abstract.Email`
        :rtype: tuple
        :return: The content and the filetype
    """
    try:
        template = MailTemplate.objects.get(codename='email_thread')
        is_html = '<html>' in template.body
    except ObjectDoesNotExist:
        raise EmailThreadTemplateNotFound('Unable to find email thread template')

    _emails = []

    for email in emails:
        _emails.append(Email(
            sender=email.sender,
            subject=email.subject,
            recipient=email.recipient,
            body=email.body.replace('\n', '<br>') if is_html else email.body,
            created=datetime.fromtimestamp(email.created),
            category=None,
            attachments=None,
        ))

    domain = ticket.service.name if ticket.service else None

    try:
        template = loader.get_template_from_string(template.body)
        context = Context({
            'publicId': ticket.publicId,
            'creationDate': ticket.creationDate,
            'domain': domain,
            'emails': _emails
        })
        content = template.render(context)
    except (TemplateEncodingError, TemplateSyntaxError) as ex:
        raise EmailThreadTemplateSyntaxError(str(ex))

    try:
        import pdfkit
        from pyvirtualdisplay import Display
        display = Display(visible=0, size=(1366, 768))
        display.start()
        content = pdfkit.from_string(content, False)
        display.stop()
        return content, 'application/pdf'
    except:
        return content.encode('utf-8'), 'text/html' if is_html else 'text/plain'


def redis_lock(key):
    """
        Decorator using redis as a lock manager

        :param str string: The redis key to monitor
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            count = 0
            while redis.exists(key):
                if count > 180:
                    raise Exception('%s seems locked' % key)
                count += 1
                sleep(1)
            redis.set(key, True)
            try:
                return func(*args, **kwargs)
            finally:
                redis.delete(key)
        return wrapper
    return decorator
