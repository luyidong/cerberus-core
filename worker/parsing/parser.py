# -*- coding: utf8 -*-
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
    Email parser for Cerberus
"""

from __future__ import unicode_literals

import glob
import hashlib
import imp
import inspect
import mimetypes
import os
import re
import sys
import time
from email.errors import HeaderParseError
from email.Header import decode_header
from email.Parser import Parser
from email.utils import mktime_tz, parsedate_tz

import html2text
from django.conf import settings
from django.db.models import ObjectDoesNotExist

CURRENTDIR = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
PARENTDIR = os.path.dirname(CURRENTDIR)
sys.path.insert(0, PARENTDIR)

import regexp
from abuse.models import Category, Provider
from utils import utils

CATEGORIES = Category.objects.all().values_list('name', flat=True)
DEFAULT_CATEGORY = 'Other'
TEMPLATE_DIR = '/templates'


class ParsedEmail(dict):
    """
        An abuse parsed_email (syntactic sugar of dict), not using namedtuple because of dynamic setattr
    """
    def __getattr__(self, name):
        if name in self:
            return self[name]
        else:
            raise AttributeError("No such attribute: " + name)

    def __setattr__(self, name, value):
        self[name] = value

    def __delattr__(self, name):
        if name in self:
            del self[name]
        else:
            raise AttributeError("No such attribute: " + name)


class EmailParser(object):
    """
        Parse email to extract usefull informations
    """

    def __init__(self):
        self._parser = Parser()
        self._html2text = html2text.HTML2Text()
        self._html2text.ignore_images = True
        self._html2text.images_to_alt = True
        self._html2text.ignore_links = True
        self._templates = self.__load_templates()

    def parse(self, content):
        """
            Parse a raw email

            :param str content: The raw email
            :rtype: `ParsedEmail`
            :returns: The parsed email
        """

        parsed_email = ParsedEmail()

        for att in ['category', 'ips', 'urls', 'fqdn', 'logs']:
            setattr(parsed_email, att, None)

        headers = self.get_headers(content)
        body, attachments = self.get_body_and_attachments(content)

        setattr(parsed_email, 'body', body)
        setattr(parsed_email, 'attachments', attachments)
        setattr(parsed_email, 'date', get_date_from_headers(headers))
        setattr(parsed_email, 'received', get_received_from_headers(headers))
        setattr(parsed_email, 'provider', get_sender_from_headers(headers))
        setattr(parsed_email, 'recipients', get_recipients_from_headers(headers))
        setattr(parsed_email, 'subject', get_subject_from_headers(headers))
        setattr(parsed_email, 'trusted', is_provider_trusted(headers))
        setattr(parsed_email, 'ack', is_email_ack(parsed_email.provider, parsed_email.subject, body))

        content_to_parse = parsed_email.subject + '\n' + parsed_email.body
        emails = [parsed_email.provider]
        if parsed_email.recipients:
            emails = parsed_email.recipients + emails

        if 'x-arf' in content_to_parse.lower():
            emails += ['x-arf']

        template = None
        for email in emails:
            template = self.get_template(email)
            if template:
                self.update_parsed_email(parsed_email, content_to_parse, template)

        # If not find any relevant informations, use default template
        if not parsed_email.urls and not parsed_email.ips:
            template = self._templates['default']
            self.update_parsed_email(parsed_email, content_to_parse, template)

        # Checking if a default category is set for this provider
        try:
            prov = Provider.objects.get(email=parsed_email.provider)
            parsed_email.category = prov.defaultCategory_id if prov.defaultCategory_id else parsed_email.category
        except (KeyError, ObjectDoesNotExist):
            pass

        # If no category, checking if provider email match a generic provider
        if parsed_email.category not in CATEGORIES:
            for reg, val in regexp.PROVIDERS_GENERIC.iteritems():  # For 123854689@spamcop for example
                if reg.match(parsed_email.provider):
                    try:
                        prov = Provider.objects.get(email=val)
                        parsed_email.category = prov.defaultCategory_id if prov.defaultCategory_id else DEFAULT_CATEGORY
                        break
                    except (KeyError, ObjectDoesNotExist):
                        parsed_email.category = DEFAULT_CATEGORY
                        break
        # Still not ?
        if parsed_email.category not in CATEGORIES:
            parsed_email.category = DEFAULT_CATEGORY

        # Provider links to online logs/parsed_emails
        if parsed_email.logs:
            attachment = {'type': 'text/plain', 'filename': 'online_logs.txt'}
            try:
                response = utils.request_wrapper(parsed_email.logs[0], method='GET', as_json=False)
                attachment['data'] = self._html2text.handle(response.text).encode('utf-8')
                parsed_email.attachments.append(attachment)
            except utils.RequestException:
                pass

        clean_parsed_email_items(parsed_email)
        return parsed_email

    def get_headers(self, raw):
        """
            Returns email SMTP headers

            :param str raw: The raw email
        """
        return self._parser.parsestr(raw, headersonly=True)

    def get_body_and_attachments(self, raw):
        """
            Get the body of the mail and retreive attachments

            :param str raw: The raw email
            :rtype: tuple
            :returns: The decoded body and a list of attachments
        """
        messages = self._parser.parsestr(raw)
        attachments = []
        body = ''

        for message in messages.walk():

            content_type = message.get_content_type().lower()
            if message.is_multipart() and content_type != 'message/rfc822':
                continue

            content_disposition = message.get('Content-Disposition')
            if content_disposition:
                content_disposition = content_disposition.decode('utf-8').encode('utf-8')

            if content_disposition and content_disposition.decode('utf-8').strip().split(';')[0].lower() == 'attachment':
                attachments.append(parse_attachment(message))
                continue

            content = message.get_payload(decode=True)
            if not content:
                content = message.as_string()

            if content:
                body += utils.decode_every_charset_in_the_world(content, message.get_content_charset())

        return body, attachments

    def get_template(self, provider):
        """
            Mail from trusted provider (spamhaus etc ...) are formatted
            For each provider, regex pattern extract useful informations
            The set of patterns are named template
            URL, IPS ...

            :param str provider: The email of the provider
            :rtype: dict:
            :returns: The corresponding parsing template if exists
        """
        template = None
        try:
            template = self._templates[provider]
        except KeyError:
            for reg, val in regexp.PROVIDERS_GENERIC.iteritems():
                if reg.match(provider):
                    try:
                        template = self._templates[val]
                        return template
                    except KeyError:
                        pass
        return template

    def update_parsed_email(self, parsed_email, content, template=None):
        """
            Get all items (IP, URL) of a parsed_email
        """
        if not template:
            template = self._templates['default']
        try:
            for key, val in template.iteritems():
                if 'pattern' in val:
                    if 'pretransform' in val:
                        res = re.findall(val['pattern'], val['pretransform'](content), re.IGNORECASE)
                    else:
                        res = re.findall(val['pattern'], content, re.IGNORECASE)
                    if res:
                        if 'transform' in val:
                            res = self.__transform(res[0])
                        setattr(parsed_email, key, res)
                elif 'value' in val:
                    setattr(parsed_email, key, val['value'])
        except AttributeError:
            pass

    def __load_templates(self):
        """
            Loads provider template from TEMPLATE_DIR

            :rtype: dict:
            :returns: All templates available in TEMPLATE_DIR
        """
        template_base = os.path.dirname(os.path.realpath(__file__)) + TEMPLATE_DIR
        modules = glob.glob(os.path.join(template_base, '*.py'))
        template_files = [os.path.basename(f)[:-3] for f in modules if not f.endswith('__init__.py')]

        templates = {}

        for template in template_files:
            infos = imp.load_source(template, os.path.join(template_base, template + '.py'))
            templates[infos.TEMPLATE['email']] = infos.TEMPLATE['regexp']

        return templates

    def __transform(self, content):
        """
            Try to get parsed_email category with defined keywords

            :param str content: The content to parse
            :rtype: str
            :returns: The category (or None if not identified)

        """
        if isinstance(content, tuple):
            text = content[0]
        else:
            text = content

        category = None
        last_count = 0
        for cat, pattern in regexp.CATEGORY.iteritems():
            count = len(re.findall(pattern, text, re.IGNORECASE))
            if count > last_count:
                last_count = count
                category = cat

        return category


def get_sender_from_headers(headers):
    """
        Get the provider aka the sender

        :param `Message` headers: The SMTP headers of the email
        :rtype: str
        :returns: The email of the sender
    """
    provider = 'unknown@provider.com'
    from_part = []
    if 'From' in headers and headers['From'] is not None:
        decodefrag = decode_header(headers['From'])
        for line, encoding in decodefrag:
            enc = 'iso-8859-11' if encoding == 'windows-874' else encoding
            enc = 'utf-8' if enc is None else enc
            from_part.append(line.decode(enc, 'replace'))
        sender = ''.join(from_part)
        line = regexp.EMAIL.search(sender)
        if line is not None:
            provider = line.group(1)
        elif 'api:' in sender:
            provider = sender.replace('<', '').replace('>', '')

    return provider.lower()


def get_recipients_from_headers(headers):
    """
        Get recipients of the email

        :param `Message` headers: The SMTP headers of the email
        :rtype: list
        :returns: The list of recipients
    """
    keys = ['To', 'Cc']
    recipients = []

    for key in keys:
        if key in headers and headers[key] is not None:
            decodefrag = decode_header(headers[key])
            for line, encoding in decodefrag:
                enc = 'utf-8' if encoding is None else encoding
                recps = line.decode(enc, 'replace').split(',')

            for recp in recps:
                line = regexp.EMAIL.search(recp)
                if line is not None:
                    recipient = str(line.group(1)).lower()
                    recipients.append(recipient)

    return recipients


def get_date_from_headers(headers):
    """
        Get date of an email

        :param `Message` headers: The SMTP headers of the email
        :rtype: datetime
        :returns: The datetime
    """
    try:
        date = headers.get('Received')
        date = parsedate_tz(date.split(';')[1].strip())
        date = mktime_tz(date)
    except (AttributeError, IndexError, KeyError, TypeError):
        date = time.time()
    return date


def get_subject_from_headers(headers):
    """
        Get the subject of an email

        :param `Message` headers: The SMTP headers of the email
        :rtype: str
        :returns: The subject of the email
    """
    subject = ''
    subject_part = []
    if 'Subject' in headers and headers['Subject'] is not None:
        try:
            decodefrag = decode_header(headers['Subject'])
        except HeaderParseError:
            return subject

        for line, encoding in decodefrag:
            enc = 'utf-8' if encoding is None or encoding == 'unknown' else encoding
            subject_part.append(utils.decode_every_charset_in_the_world(line, enc))

        subject = ''.join(subject_part)[:1023]
    return subject


def get_received_from_headers(headers):
    """
        Parse 'received from' from headers

        :param `Message` headers: The SMTP headers of the email
        :rtype: list
        :returns: The list of received emails
    """
    result = []
    if headers.get('Received'):
        for current in headers.get_all('Received'):
            if re.search('^from', current):
                # Only keep dns and ip once
                current = utils.decode_every_charset_in_the_world(current)
                final = current.splitlines()[0].split(";")[0].replace("from ", "").replace("localhost", "").replace("by", "")
                result.append(final)

    return result


def is_provider_trusted(headers):
    """
        Check if there is the trusted magic_smtp_header in headers

        :param `Message` headers: The SMTP headers of the email
        :rtype: bool
        :returns: If the header is present or not
    """
    if not settings.GENERAL_CONFIG.get('magic_smtp_header'):
        return False

    return settings.GENERAL_CONFIG['magic_smtp_header'] in headers


def parse_attachment(part):
    """
        Get attachments of an email

        :param `Message` part: A `Message`
        :rtype: list
        :returns: The list of attachments
    """
    attachment = {}
    attachment['type'] = part.get_content_type()

    if attachment['type'].lower() in ['message/rfc822', 'message/delivery-status']:
        attachment['data'] = str(part)
    else:
        attachment['data'] = part.get_payload(decode=True)

    filename = part.get_filename()

    if not filename:
        filename = hashlib.sha1(attachment['data']).hexdigest()
        if attachment['type']:
            extension = mimetypes.guess_extension(attachment['type'])
            if extension:
                filename += extension

    attachment['filename'] = utils.decode_every_charset_in_the_world(filename)
    return attachment


def clean_parsed_email_items(parsed_email):
    """
        Remove extra stuff from Report items

        :param `ParsedEmail` parsed_email: The parsed email
    """
    for attrib in [a for a in ['urls', 'ips', 'fqdn'] if getattr(parsed_email, a)]:
        setattr(parsed_email, attrib, [__clean_item(item) for item in getattr(parsed_email, attrib)])

    # If parsed ip/fqdn are present in url, only keeping url
    if getattr(parsed_email, 'urls') and len(parsed_email.urls):
        urls = ' '.join(parsed_email.urls)
        if getattr(parsed_email, 'ips'):
            parsed_email.ips = [ip_addr for ip_addr in parsed_email.ips if not re.search(regexp.PROTO_RE + re.escape(ip_addr), urls, re.I)]
        if getattr(parsed_email, 'fqdn'):
            parsed_email.fqdn = [fqdn for fqdn in parsed_email.fqdn if not re.search(regexp.PROTO_RE + re.escape(fqdn), urls, re.I)]

    # Clean duplicates
    for att in parsed_email.keys():
        if isinstance(getattr(parsed_email, att), list) and not all(isinstance(x, dict) for x in getattr(parsed_email, att)):
            setattr(parsed_email, att, list(set(getattr(parsed_email, att))))


def __clean_item(item):
    """ Remove extra stuff from item

        :param str item: A `ParsedEmail` item
        :rtype: str
        :returns: The cleaned item
    """
    item = item.strip()
    item = item.replace(' ', '')

    for key, reg in regexp.DEOBFUSCATE_URL.iteritems():
        item = reg.sub(key, item)

    item = item.replace('[.]', '.')
    item = re.sub(r'([^:])/{2,}', r'\1/', item)
    return item


def is_email_ack(provider, subject, body):
    """
        Checking if email is not an automatic answer

        :param str provider: The email of the provider
        :param str subject: The subject of the email
        :param str body: The body of the email
        :rtype: bool
        :returns: If it's a automatic answer
    """
    resp = False
    if provider in regexp.ACK_RE:
        subject_re = regexp.ACK_RE[provider]['SUBJECT']
        body_re = regexp.ACK_RE[provider]['BODY']
        if re.search(body_re, body, re.IGNORECASE) and re.search(subject_re, subject, re.IGNORECASE):
            resp = True

    return resp