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
    Defined GlobalSchedulingAlgorithm (standard) class
"""

import operator

from datetime import datetime, timedelta

from django.db.models import Count, Q

from abuse.models import AbusePermission, Defendant, Ticket, User
from api.controllers.scheduling.abstract import TicketSchedulingAlgorithmBase

TODO_TICKET_STATUS_FILTERS = (['Open'],)
TODO_TICKET_PRIORITY_FILTERS = ('Normal', 'Low')
TICKET_FIELDS = [fld.name for fld in Ticket._meta.fields]
USER_FILTERS_BEGINNER_PRIORITY = ('Low', 'Normal')


class LimitedOpenSchedulingAlgorithm(TicketSchedulingAlgorithmBase):
    """
        Class defining standard `abuse.models.Ticket` scheduling algorithm
    """
    def count(self, **kwargs):

        if kwargs.get('where'):
            where = kwargs['where']
            count = Ticket.objects.filter(
                where,
                status='Open',
                priority__in=TODO_TICKET_PRIORITY_FILTERS
            ).order_by('id').distinct().count()
        else:
            count = Ticket.objects.filter(
                status__in='Open',
                priority__in=TODO_TICKET_PRIORITY_FILTERS
            ).order_by('id').distinct().count()

        return count

    def get_tickets(self, user=None, **kwargs):
        """
            Returns available `abuse.models.Ticket` according to scheduling algorithm

            Tickets selections:

            By priority of ticket status  (see TODO_TICKET_STATUS_FILTERS)
                By priority of ticket priorities (see TODO_TICKET_PRIORITY_FILTERS)
                    By user, others then treatedBy nobody
        """
        filters = {}
        if kwargs.get('filters'):
            filters = kwargs['filters']

        try:
            limit = int(filters['paginate']['resultsPerPage'])
            offset = int(filters['paginate']['currentPage'])
        except KeyError:
            limit = 10
            offset = 1

        where = get_user_filters(user)
        order_by = ['modificationDate', '-reportTicket__tags__level']

        if filters.get('onlyUnassigned'):
            where.append(Q(treatedBy=None))
            treated_by_filters = [{'treatedBy': None}]
        else:
            treated_by_filters = get_treated_by_filters(user)

        # Aggregate all filters
        where = reduce(operator.and_, where)

        nb_record = Ticket.objects.filter(
            where,
            status='Open',
            priority__in=TODO_TICKET_PRIORITY_FILTERS
        ).distinct().count()

        res = []
        ids = set()

        for ticket_status in TODO_TICKET_STATUS_FILTERS:
            for priority in TODO_TICKET_PRIORITY_FILTERS:
                for filters in treated_by_filters:
                    tickets = get_specific_filtered_todo_tickets(where, ids, priority, ticket_status, filters, order_by, limit, offset)
                    ids.update([t['id'] for t in tickets])
                    res.extend(tickets)
                    if len(res) > limit * offset:
                        return res[(offset - 1) * limit:limit * offset], nb_record

        return res[(offset - 1) * limit:limit * offset], nb_record


def get_treated_by_filters(user):

    users = list(set(User.objects.all().values_list('username', flat=True)))
    others_users = [username for username in users if username != user.username]

    treated_by_filters = [
        {
            'treatedBy__username': user.username,
        },
        {
            'treatedBy__username__in': others_users,
        },
        {
            'treatedBy': None,
        },
    ]
    return treated_by_filters


def get_specific_filtered_todo_tickets(where, ids, priority, status, treated_by, order_by, limit, offset):

    tickets = Ticket.objects.filter(
        where,
        ~Q(id__in=ids),
        priority=priority,
        status__in=status,
        **treated_by
    ).values(
        *TICKET_FIELDS
    ).order_by(
        *order_by
    ).annotate(
        attachedReportsCount=Count('reportTicket')
    ).distinct()[:limit * offset]

    res = []
    for ticket in tickets:
        if ticket.get('defendant'):
            defendant = Defendant.objects.get(id=ticket['defendant'])
            count = defendant.ticketDefendant.filter(creationDate__lte=(datetime.now() - timedelta(days=90))).count()
            if count == 0 and defendant.details.creationDate < (datetime.now() - timedelta(days=15)):
                res.append(ticket)
        else:
            res.append(ticket)
    return res


def get_user_filters(user):
    """
        Filter allowed category for this user
    """
    where = [Q()]
    user_specific_where = []
    abuse_permissions = AbusePermission.objects.filter(user=user.id)

    for perm in abuse_permissions:
        if perm.profile.name == 'Expert':
            user_specific_where.append(Q(category=perm.category))
        elif perm.profile.name == 'Advanced':
            user_specific_where.append(Q(category=perm.category, confidential=False))
        elif perm.profile.name == 'Beginner':
            user_specific_where.append(Q(
                priority__in=USER_FILTERS_BEGINNER_PRIORITY,
                category=perm.category,
                confidential=False,
                escalated=False,
                moderation=False
            ))

    if len(user_specific_where):
        user_specific_where = reduce(operator.or_, user_specific_where)
        where.append(user_specific_where)
    else:
        # If no category allowed
        where.append(Q(category=None))

    return where
