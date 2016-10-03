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
    Report functions for worker
"""
import hashlib
from collections import Counter
from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import ObjectDoesNotExist, Q

import common
import database
from abuse.models import (AttachedDocument, Defendant, Proof, Report,
                          ReportItem, ReportThreshold, Service, Ticket, User)
from adapters.dao.customer.abstract import CustomerDaoException
from adapters.services.mailer.abstract import MailerServiceException
from adapters.services.search.abstract import SearchServiceException
from factory.factory import ImplementationFactory, ReportWorkflowFactory
from parsing.parser import EmailParser
from utils import pglocks, schema, utils
from worker import Logger

Parser = EmailParser()


def create_from_email(email_content=None, filename=None, lang='EN', send_ack=False):
    """
        Create Cerberus report(s) based on email content

        If send_ack is True and report is attached to a ticket,
        then an acknowledgement is sent to the email provider.

        :param str email_content: The raw email content
        :param str filename: The name of the raw email file
        :param str lang: Langage to use if send_ack is True
        :param bool send_ack: If an acknowledgment have to be sent to provider
        :raises CustomerDaoException: if exception while identifying defendants from items
        :raises MailerServiceException: if exception while updating ticket's emails
        :raises StorageServiceException: if exception while accessing storage
    """
    # This function use a lock/commit_on_succes on db when creating reports
    #
    # Huge blocks of code are under transaction because it's important to
    # rollback if ANYTHING goes wrong in the report creation workflow.
    #
    # Concurrent transactions (with multiple workers), on defendant/service creation
    # can result in unconsistent data, So a pg_lock is used.
    #
    # `abuse.models.Defendant` and `abuse.models.Service` HAVE to be unique.

    if not email_content:
        Logger.error(unicode('Missing email content'))
        return

    if not filename:  # Worker have to push email to Storage Service
        filename = hashlib.sha256(email_content).hexdigest()
        __save_email(filename, email_content)

    # Parse email content
    abuse_report = Parser.parse(email_content)
    Logger.debug(unicode('New email from %s' % (abuse_report.provider)), extra={'from': abuse_report.provider, 'action': 'new email'})

    # Check if provider is not blacklisted
    if abuse_report.provider in settings.PARSING['providers_to_ignore']:
        Logger.error(unicode('Provider %s is blacklisted, skipping ...' % (abuse_report.provider)))
        return

    # Check if it's an answer to a ticket
    ticket, category, recipient = ImplementationFactory.instance.get_singleton_of('MailerServiceBase').is_email_ticket_answer(abuse_report)
    if all((ticket, category, recipient)):  # OK it's an anwser, updating ticket and exiting
        _update_ticket_if_answer(ticket, category, recipient, abuse_report, filename)
        return

    # Check if items are linked to customer and get corresponding services
    try:
        services = ImplementationFactory.instance.get_singleton_of('CustomerDaoBase').get_services_from_items(
            urls=abuse_report.urls,
            ips=abuse_report.ips,
            fqdn=abuse_report.fqdn
        )
        schema.valid_adapter_response('CustomerDaoBase', 'get_services_from_items', services)
    except CustomerDaoException as ex:
        Logger.error(unicode('Exception while identifying defendants from items for mail %s -> %s ' % (filename, str(ex))))
        raise CustomerDaoException(ex)

    # Create report(s) with identified services
    if not services:
        created_reports = [__create_without_services(abuse_report, filename)]
    else:
        with pglocks.advisory_lock('cerberus_lock'):
            __create_defendants_and_services(services)
        created_reports = __create_with_services(abuse_report, filename, services)

    # Upload attachments
    if abuse_report.attachments:
        __save_attachments(created_reports, filename, abuse_report.attachments)

    # Send acknowledgement to provider (only if send_ack = True and report is attached to a ticket)
    for report in created_reports:
        if send_ack and report.ticket:
            try:
                __send_ack(report, lang=lang)
            except MailerServiceException as ex:
                raise MailerServiceException(ex)

    # Index to SearchService
    if ImplementationFactory.instance.is_implemented('SearchServiceBase'):
        __index_report_to_searchservice(abuse_report, filename, [rep.id for rep in created_reports])

    Logger.info(unicode('All done successfully for email %s' % (filename)))


@transaction.atomic
def __create_without_services(abuse_report, filename):
    """
        Create report in Cerberus

        :param `worker.parsing.parser.ParsedEmail` abuse_report: The `worker.parsing.parser.ParsedEmail`
        :param str filename: The filename of the email
        :rtype: `abuse.models.Report`
        :returns: The Cerberus `abuse.models.Report`
    """
    provider = database.get_or_create_provider(abuse_report.provider)
    trusted = True if provider.trusted or abuse_report.trusted else False
    status = 'ToValidate' if trusted else 'New'

    report = Report.objects.create(**{
        'provider': provider,
        'receivedDate': datetime.fromtimestamp(abuse_report.date),
        'subject': abuse_report.subject,
        'body': abuse_report.body,
        'category': database.get_category(abuse_report.category),
        'filename': filename,
        'status': status,
    })

    __add_report_tags(report, abuse_report.recipients)
    autoarchive, _, _ = __get_attributes_based_on_tags(report, abuse_report.recipients)
    database.log_new_report(report)

    # If report is not attached within 30 days -> archived
    if report.status == 'New':
        utils.scheduler.enqueue_in(
            timedelta(days=settings.GENERAL_CONFIG['report_timeout']),
            'report.archive_if_timeout',
            report_id=report.id
        )

    if autoarchive:
        report.status = 'Archived'

    report.save()
    return report


def __create_defendants_and_services(services):

    for data in services:  # For identified (service, defendant, items) tuple

        data['defendant'] = database.get_or_create_defendant(data['defendant'])
        data['service'] = database.get_or_create_service(data['service'])


@transaction.atomic
def __create_with_services(abuse_report, filename, services):
    """
        Create report(s), ticket(s), item(s), defendant(s), service(s), attachment(s) in Cerberus

        :param `ParsedEmail` abuse_report: The `ParsedEmail`
        :param str filename: The filename of the email
        :param dict services: The identified service(s) (see adapters/dao/customer/abstract.py)
        :rtype: list
        :returns: The list of Cerberus `abuse.models.Report` created
    """
    created_reports = []

    for data in services:  # For identified (service, defendant, items) tuple

        report = __create_without_services(abuse_report, filename)
        created_reports.append(report)
        report.defendant = data['defendant']
        report.service = data['service']
        report.save()

        if report.status == 'Archived':  # because autoarchive tag
            continue

        _, attach_only, no_phishtocheck = __get_attributes_based_on_tags(report, abuse_report.recipients)
        __insert_items(report.id, data['items'])

        # The provider or the way we received the report
        trusted = True if report.provider.trusted or abuse_report.trusted else False

        # Looking for existing open ticket for same (service, defendant, category)
        ticket = None
        if all((report.defendant, report.category, report.service)):
            msg = 'Looking for opened ticket for (%s, %s, %s)'
            Logger.debug(unicode(msg % (report.defendant.customerId, report.category.name, report.service.name)))
            ticket = database.search_ticket(report.defendant, report.category, report.service)

        # Checking specific processing workflow
        is_workflow_applied = False
        for workflow in ReportWorkflowFactory.instance.registered_instances:
            if workflow.identify(report, ticket, is_trusted=trusted):
                is_workflow_applied = workflow.apply(report, ticket, trusted, no_phishtocheck)
                if is_workflow_applied:
                    database.set_report_specificworkflow_tag(report, str(workflow.__class__.__name__))
                    Logger.debug(unicode('Specific workflow %s applied' % str(workflow.__class__.__name__)))
                    break

        if is_workflow_applied:
            continue

        # If attach report only and no ticket found, continue
        if not ticket and attach_only:
            report.status = 'Archived'
            report.save()
            continue

        # Create ticket if trusted
        new_ticket = False
        if not ticket and trusted:
            ticket = database.create_ticket(report.defendant, report.category, report.service, priority=report.provider.priority)
            new_ticket = True

        if ticket:
            report.ticket = Ticket.objects.get(id=ticket.id)
            report.status = 'Attached'
            report.save()
            database.set_ticket_higher_priority(report.ticket)
            database.log_action_on_ticket(
                ticket=ticket,
                action='attach_report',
                report=report,
                new_ticket=new_ticket
            )

    return created_reports


def __index_report_to_searchservice(parsed_email, filename, reports_id):
    """ Index a report to the SearchService
    """
    try:
        Logger.debug(unicode('Pushing email %s document to SearchService' % (filename)))
        ImplementationFactory.instance.get_singleton_of('SearchServiceBase').index_email(
            parsed_email,
            filename,
            reports_id
        )
    except SearchServiceException as ex:
        # Not fatal => don't stop current routine
        Logger.error(unicode('Unable to index mail %s in SearchService -> %s' % (filename, ex)))


def __send_ack(report, lang=None):
    """ Send acknoledgement to provider

        :param `abuse.models.Report` report: A `abuse.models.Report` instance
        :param string lang: The langage to use
    """
    if settings.TAGS['no_autoack'] not in report.provider.tags.all().values_list('name', flat=True):
        common.send_email(
            report.ticket,
            [report.provider.email],
            settings.CODENAMES['ack_received'],
            lang=lang,
            acknowledged_report_id=report.id,
        )

    report.ticket = Ticket.objects.get(id=report.ticket.id)
    report.save()


def __insert_items(report_id, items):
    """ Insert report's items for to database

        :param int report_id: The id of the report
        :param dict items: A dict of list containing the items
    """
    for item_type in ['urls', 'ips', 'fqdn']:
        nature = item_type.replace('s', '').upper()
        if items.get(item_type):
            for item in items[item_type]:
                item_dict = {
                    'itemType': nature,
                    'report_id': report_id,
                    'rawItem': item[:4000],
                }
                item_dict.update(utils.get_reverses_for_item(item, nature=nature))
                ReportItem.objects.create(**item_dict)


def __save_attachments(reports, filename, attachments):
    """ Upload email attachments to StorageService and keep a reference in Cerberus

        :param list reports: A list of `abuse.models.Report` instance
        :param list attachments: A list of dict {'filename': 'test.pdf', 'data': '...', 'type': 'application/pdf'}
    """
    for attachment in attachments[:50]:  # Slice 50 to avoid denial of service

        storage_filename = filename + '-attach-'
        storage_filename = storage_filename.encode('utf-8')
        storage_filename = storage_filename + attachment['filename']

        with ImplementationFactory.instance.get_instance_of('StorageServiceBase', settings.GENERAL_CONFIG['email_storage_dir']) as cnx:
            cnx.write(storage_filename, attachment['data'])

        attachment_obj = AttachedDocument.objects.create(
            name=attachment['filename'],
            filename=storage_filename,
            filetype=attachment['type'],
        )

        for report in reports:
            report.attachments.add(attachment_obj)


def __save_email(filename, email):
    """
        Push email storage service

        :param str filename: The filename of the email
        :param str email: The content of the email
    """
    with ImplementationFactory.instance.get_instance_of('StorageServiceBase', settings.GENERAL_CONFIG['email_storage_dir']) as cnx:
        cnx.write(filename, email)
        Logger.info(unicode('Email %s pushed to Storage Service' % (filename)))


def __add_report_tags(report, recipients):
    """
        Add tags to report based on provider, subject etc ...

        :param `abuse.models.Report` report: A `abuse.models.Report` instance
        :param list recipients: The list of recipients
    """
    tags = database.get_tags(report.provider, recipients, report.subject, report.body)

    for tag in tags:
        if tag.tagType == 'Report':
            report.tags.add(tag)


def __get_attributes_based_on_tags(report, recipients):
    """
        Get specific attributes based on provider's tags

        :param `abuse.models.Report` report: A `abuse.models.Report` instance
        :param list recipients: The list of recipients
        :rtype: tuple
        :returns: tuple of bool (autoarchive, attach_only, no_phishtocheck)
    """
    autoarchive = attach_only = no_phishtocheck = False
    tags = database.get_tags(report.provider, recipients, report.subject, report.body)

    for tag in tags:
        if tag.tagType == 'Provider':
            if tag.name == settings.TAGS['autoarchive']:
                autoarchive = True
            if tag.name == settings.TAGS['attach_only']:
                attach_only = True
            if tag.name == settings.TAGS['no_phishtocheck']:
                no_phishtocheck = True

    return autoarchive, attach_only, no_phishtocheck


def _update_ticket_if_answer(ticket, category, recipient, abuse_report, filename):
    """
        If the email is an answer to a cerberus ticket:

        - update ticket status
        - cancel all pending ServiceAction jobs and ticket.timeout jobs
        - append response to ticket's email thread
        - save attachments

        :param `abuse.models.Ticket` ticket: A Cerberus `abuse.models.Ticket` instance
        :param str category: The category of the answer ('Defendant', 'Plaintiff' or 'Other)
        :param str recipient: The recipient of the answer
        :param `worker.parsing.parser.ParsedEmail` abuse_report: The ParsedEmail
        :param str filename: The filename of the email
    """
    Logger.debug(
        unicode('New %s answer from %s for ticket %s' % (category, abuse_report.provider, ticket.id)),
        extra={
            'from': abuse_report.provider,
            'action': 'new answer',
            'hash': filename,
            'ticket': ticket.id,
        }
    )
    actions = [{'ticket': ticket, 'action': 'receive_email', 'email': abuse_report.provider}]

    try:
        if ticket.treatedBy.operator.role.modelsAuthorizations['ticket'].get('unassignedOnAnswer'):
            ticket.treatedBy = None
    except (AttributeError, KeyError, ObjectDoesNotExist, ValueError):
        pass

    if not abuse_report.ack:

        ticket.previousStatus = ticket.status
        ticket.status = 'Answered'
        ticket.snoozeStart = None
        ticket.snoozeDuration = None
        ticket.reportTicket.all().update(status='Attached')
        ticket.save()
        actions.append({'ticket': ticket, 'action': 'change_status', 'previous_value': ticket.previousStatus, 'new_value': ticket.status})
        _cancel_ticket_jobs(ticket)

    for action in actions:
        database.log_action_on_ticket(**action)

    ImplementationFactory.instance.get_singleton_of('MailerServiceBase').attach_external_answer(
        ticket,
        abuse_report.provider,
        recipient,
        abuse_report.subject,
        abuse_report.body,
        category
    )

    if abuse_report.attachments:
        __save_attachments(
            [ticket.reportTicket.all()[0]],
            filename,
            abuse_report.attachments,
        )


def _cancel_ticket_jobs(ticket):

    for job in ticket.jobs.all():
        if job.asynchronousJobId in utils.scheduler:
            utils.scheduler.cancel(job.asynchronousJobId)
            job.status = 'cancelled by answered'
            job.save()

    for job in utils.scheduler.get_jobs():
        if job.func_name == 'ticket.timeout' and job.kwargs['ticket_id'] == ticket.id:
            utils.scheduler.cancel(job.id)


def archive_if_timeout(report_id=None):
    """
        Archived report if not attached

        :param int report_id: The report id
    """
    if not report_id:
        Logger.error(unicode('Invalid parameters submitted [report_id=%d]' % (report_id)))
        return

    try:
        report = Report.objects.get(id=report_id)
    except (ObjectDoesNotExist, ValueError):
        Logger.error(unicode('Report %d cannot be found in DB. Skipping...' % (report_id)))
        return

    if report.status != 'New':
        Logger.error(unicode('Report %d not New, status : %s , Skipping ...' % (report_id, report.status)))
        return

    report.ticket = None
    report.status = 'Archived'
    report.save()
    Logger.info(unicode('Report %d successfully archived' % (report_id)))


def create_ticket_with_threshold():
    """
        Automatically creates ticket if there are more than `abuse.models.ReportThreshold.threshold`
        new reports created during `abuse.models.ReportThreshold.interval` (days) for same (category/defendant/service)
    """
    log_msg = 'threshold : Checking report threshold for category %s, threshold %d, interval %d days'

    for thres in ReportThreshold.objects.all():
        Logger.info(unicode(log_msg % (thres.category.name, thres.threshold, thres.interval)))
        reports = __get_threshold_reports(thres.category, thres.interval)
        reports = Counter(reports)
        for data, count in reports.iteritems():
            nb_tickets = Ticket.objects.filter(
                ~Q(status='Closed'),
                defendant__customerId=data[0],
                service__id=data[1],
            ).count()
            if count >= thres.threshold and not nb_tickets:
                ticket = __create_threshold_ticket(data, thres)
                Logger.info(unicode('threshold: tuple %s match, ticket %s has been created' % (str(data), ticket.id)))


def __get_threshold_reports(category, delta):

    reports = Report.objects.filter(
        ~Q(defendant=None),
        ~Q(service=None),
        category=category,
        status='New',
        receivedDate__gte=datetime.now() - timedelta(days=delta),
    ).values_list(
        'defendant__customerId',
        'service__id'
    )
    return reports


def __create_threshold_ticket(data, thres):

    service = Service.objects.filter(id=data[1]).last()
    defendant = Defendant.objects.filter(customerId=data[0]).last()
    ticket = database.create_ticket(defendant, thres.category, service)
    database.log_action_on_ticket(
        ticket=ticket,
        action='create_threshold',
        threshold_count=thres.threshold,
        threshold_interval=thres.interval,
    )
    return ticket


@transaction.atomic
def reparse_validated(report_id=None, user_id=None):
    """
        Reparse now validated `abuse.models.Report`

        :param int report_id: A Cerberus `abuse.models.Report` id
        :param int user_id: A Cerberus `abuse.models.User` id
    """
    try:
        report = Report.objects.get(id=report_id)
        user = User.objects.get(id=user_id)
    except (ObjectDoesNotExist, ValueError):
        Logger.error(unicode('Report %d cannot be found in DB. Skipping...' % (report_id)))
        return

    if not report.defendant or not report.service:
        _create_closed_ticket(report, user)
    else:
        _reinject_validated(report, user)

    Logger.error(unicode('Report %d successfully processed' % (report_id)))


def _reinject_validated(report, user):

    trusted = True
    ticket = None
    if all((report.defendant, report.category, report.service)):
        msg = 'Looking for opened ticket for (%s, %s, %s)'
        Logger.debug(unicode(msg % (report.defendant.customerId, report.category.name, report.service.name)))
        ticket = database.search_ticket(report.defendant, report.category, report.service)

    # Checking specific processing workflow
    for workflow in ReportWorkflowFactory.instance.registered_instances:
        if workflow.identify(report, ticket, is_trusted=trusted) and workflow.apply(report, ticket, trusted, False):
            Logger.debug(unicode('Specific workflow %s applied' % (str(workflow.__class__.__name__))))
            return

    # Create ticket if trusted
    new_ticket = False
    if not ticket:
        ticket = database.create_ticket(report.defendant, report.category, report.service, priority=report.provider.priority)
        new_ticket = True

    if ticket:
        report.ticket = Ticket.objects.get(id=ticket.id)
        report.status = 'Attached'
        report.save()
        database.set_ticket_higher_priority(report.ticket)
        database.log_action_on_ticket(
            ticket=ticket,
            action='attach_report',
            report=report,
            new_ticket=new_ticket,
            user=user
        )

        try:
            __send_ack(report, lang='EN')
        except MailerServiceException as ex:
            raise MailerServiceException(ex)


def _create_closed_ticket(report, user):

    report.ticket = common.create_ticket(report, attach_new=False)
    report.save()

    # Add temp proof(s) for mail content
    temp_proofs = []
    if not report.ticket.proof.count():
        temp_proofs = common.get_temp_proofs(report.ticket)

    # Send email to Provider
    try:
        validate_email(report.provider.email.strip())
        Logger.info(unicode('Sending email to provider'))
        common.send_email(report.ticket, [report.provider.email], settings.CODENAMES['not_managed_ip'])
        report.ticket.save()
        Logger.info(unicode('Mail sent to provider'))
        ImplementationFactory.instance.get_singleton_of('MailerServiceBase').close_thread(report.ticket)

        # Delete temp proof(s)
        for proof in temp_proofs:
            Proof.objects.filter(id=proof.id).delete()
    except (AttributeError, TypeError, ValueError, ValidationError):
        pass

    common.close_ticket(report, resolution_codename=settings.CODENAMES['invalid'], user=user)
    Logger.info(unicode('Ticket %d and report %d closed' % (report.ticket.id, report.id)))
