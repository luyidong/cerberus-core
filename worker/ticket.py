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
    Ticket functions for worker
"""

import hashlib
import inspect
from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address
from django.db import transaction
from django.db.models import ObjectDoesNotExist
from django.template import Context, loader

import database

from abuse.models import Category, Report, ReportItem, Resolution, Ticket, User
from adapters.dao.customer.abstract import CustomerDaoException
from factory.factory import ImplementationFactory
from utils import pglocks, schema, utils
from worker import Logger


def delay_jobs(ticket=None, delay=None, back=True):
    """
        Delay pending jobs for given `abuse.models.Ticket`

        :param `abuse.models.Ticket` ticket: The Cerberus ticket
        :param int delay: Postpone duration
        :param bool back: In case of unpause, reschedule jobs with effectively elapsed time
    """
    if not delay:
        Logger.error(unicode('Missing delay. Skipping...'))
        return

    if not isinstance(ticket, Ticket):
        try:
            ticket = Ticket.objects.get(id=ticket)
        except (AttributeError, ObjectDoesNotExist, TypeError, ValueError):
            Logger.error(unicode('Ticket %d cannot be found in DB. Skipping...' % (ticket)))
            return

    list_of_job_instances = utils.scheduler.get_jobs(
        until=timedelta(days=5),
        with_times=True
    )

    for job in ticket.jobs.all():
        if job.asynchronousJobId in utils.scheduler:
            for scheduled_job in list_of_job_instances:
                if scheduled_job[0].id == job.asynchronousJobId:
                    if back:
                        date = scheduled_job[1] - delay
                    else:
                        date = scheduled_job[1] + delay
                    utils.scheduler.change_execution_time(
                        scheduled_job[0],
                        date
                    )
                    break


def mass_contact(ip_address=None, category=None, campaign_name=None, email_subject=None, email_body=None, user_id=None):
    """
        Try to identify customer based on `ip_address`, creates Cerberus ticket
        then send email to customer and finally close ticket.

        The use case is: a trusted provider sent you a list of vulnerable DNS servers (DrDOS amp) for example.
        To prevent abuse on your network, you notify customer of this vulnerability.
    """
    # Check params
    _, _, _, values = inspect.getargvalues(inspect.currentframe())
    if not all(values.values()):
        Logger.error(unicode('invalid parameters submitted %s' % str(values)))
        return

    try:
        validate_ipv46_address(ip_address)
    except (TypeError, ValidationError):
        Logger.error(unicode('invalid ip addresses submitted'))
        return

    # Get Django model objects
    try:
        category = Category.objects.get(name=category)
        user = User.objects.get(id=user_id)
    except (AttributeError, ObjectDoesNotExist, TypeError):
        Logger.error(unicode('invalid user or category'))
        return

    # Identify service for ip_address
    try:
        services = ImplementationFactory.instance.get_singleton_of('CustomerDaoBase').get_services_from_items(ips=[ip_address])
        schema.valid_adapter_response('CustomerDaoBase', 'get_services_from_items', services)
    except CustomerDaoException as ex:
        Logger.error(unicode('Exception while identifying defendants for ip %s -> %s ' % (ip_address, str(ex))))
        raise CustomerDaoException(ex)

    # Create report/ticket
    if services:
        Logger.debug(unicode('creating report/ticket for ip address %s' % (ip_address)))
        with pglocks.advisory_lock('cerberus_lock'):
            __create_contact_tickets(services, campaign_name, ip_address, category, email_subject, email_body, user)
    else:
        Logger.debug(unicode('no service found for ip address %s' % (ip_address)))


@transaction.atomic
def __create_contact_tickets(services, campaign_name, ip_address, category, email_subject, email_body, user):

    # Create fake report
    report_subject = 'Campaign %s for ip %s' % (campaign_name, ip_address)
    report_body = 'Campaign: %s\nIP Address: %s\n' % (campaign_name, ip_address)
    filename = hashlib.sha256(report_body.encode('utf-8')).hexdigest()
    __save_email(filename, report_body)

    for data in services:  # For identified (service, defendant, items) tuple

        actions = []

        # Create report
        report = Report.objects.create(**{
            'provider': database.get_or_create_provider('mass_contact'),
            'receivedDate': datetime.now(),
            'subject': report_subject,
            'body': report_body,
            'category': category,
            'filename': filename,
            'status': 'Archived',
            'defendant': database.get_or_create_defendant(data['defendant']),
            'service': database.get_or_create_service(data['service']),
        })
        database.log_new_report(report)

        # Create item
        item_dict = {'itemType': 'IP', 'report_id': report.id, 'rawItem': ip_address}
        item_dict.update(utils.get_reverses_for_item(ip_address, nature='IP'))
        ReportItem.objects.create(**item_dict)

        # Create ticket
        ticket = database.create_ticket(
            report.defendant,
            report.category,
            report.service,
            priority=report.provider.priority,
            attach_new=False,
        )
        database.add_mass_contact_tag(ticket, campaign_name)
        actions.append('create this ticket with mass contact campaign %s' % (campaign_name))
        report.ticket = ticket
        report.save()
        Logger.debug(unicode(
            'ticket %d successfully created for (%s, %s)' % (ticket.id, report.defendant.customerId, report.service.name)
        ))

        # Send email to defendant
        __send_mass_contact_email(ticket, email_subject, email_body)
        actions.append('send an email to %s' % (report.defendant.details.email))

        # Close ticket/report
        ticket.resolution = Resolution.objects.get(codename=settings.CODENAMES['fixed_customer'])
        ticket.previousStatus = ticket.status
        ticket.status = 'Closed'
        ticket.save()
        actions.append('change status from %s to %s, reason : %s' % (ticket.previousStatus, ticket.status, ticket.resolution.codename))

        for action in actions:
            database.log_action_on_ticket(ticket, action, user=user)


def __send_mass_contact_email(ticket, email_subject, email_body):

    template = loader.get_template_from_string(email_subject)
    context = Context({
        'publicId': ticket.publicId,
        'service': ticket.service.name.replace('.', '[.]'),
        'lang': ticket.defendant.details.lang,
    })
    subject = template.render(context)

    template = loader.get_template_from_string(email_body)
    context = Context({
        'publicId': ticket.publicId,
        'service': ticket.service.name.replace('.', '[.]'),
        'lang': ticket.defendant.details.lang,
    })
    body = template.render(context)

    ImplementationFactory.instance.get_singleton_of('MailerServiceBase').send_email(
        ticket,
        ticket.defendant.details.email,
        subject,
        body
    )


def __save_email(filename, email):
    """
        Push email storage service

        :param str filename: The filename of the email
        :param str email: The content of the email
    """
    with ImplementationFactory.instance.get_instance_of('StorageServiceBase', settings.GENERAL_CONFIG['email_storage_dir']) as cnx:
        cnx.write(filename, email.encode('utf-8'))
        Logger.info(unicode('Email %s pushed to Storage Service' % (filename)))
