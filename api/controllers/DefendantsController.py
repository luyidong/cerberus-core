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
    Cerberus defendant manager
"""

from time import mktime, time

from django.core.exceptions import FieldError, MultipleObjectsReturned
from django.db import IntegrityError
from django.db.models import Q, Count, ObjectDoesNotExist
from django.forms.models import model_to_dict

import GeneralController
from abuse.models import Category, Defendant, DefendantComment, Stat, Tag
from adapters.dao.customer.abstract import CustomerDaoException
from factory.factory import ImplementationFactory

DEFENDANT_FIELDS = [fld.name for fld in Defendant._meta.fields]


def index():
    """
        Get all defendants
    """
    defendants = Defendant.objects.filter().values('tags', *DEFENDANT_FIELDS)

    for defendant in defendants:
        if defendant.get('creationDate', None):
            defendant['creationDate'] = defendant['creationDate'].strftime("%d/%m/%y")
        tags = Defendant.objects.get(id=defendant['id']).tags.all()
        defendant['tags'] = [model_to_dict(tag) for tag in tags]

    return 200, [dict(d) for d in defendants]


def show(defendant_id):
    """
        Get defendant
    """
    try:
        defendant = Defendant.objects.get(id=defendant_id)
    except (ObjectDoesNotExist, ValueError):
        return 404, {'status': 'Not Found', 'code': 404}

    defendant = model_to_dict(defendant)
    defendant_infos = None

    try:
        defendant_infos = ImplementationFactory.instance.get_singleton_of('CustomerDaoBase').get_customer_infos(defendant['customerId'])
    except CustomerDaoException:
        pass

    if defendant_infos:
        for key in ['state', 'email']:
            if defendant_infos.get(key) and defendant_infos.get(key) != defendant.get(key):
                defendant[key] = defendant_infos.get(key)

    # Add comments
    defendant['comments'] = [{
        'id': c.comment.id,
        'user': c.comment.user.username,
        'date': mktime(c.comment.date.timetuple()),
        'comment': c.comment.comment
    } for c in DefendantComment.objects.filter(defendant=defendant_id).order_by('-comment__date')]

    if defendant.get('creationDate', None):
        defendant['creationDate'] = defendant['creationDate'].strftime("%d/%m/%y")

    # Add tags
    tags = Defendant.objects.get(id=defendant['id']).tags.all()
    defendant['tags'] = [model_to_dict(tag) for tag in tags]

    return 200, defendant


def add_tag(defendant_id, body, user):
    """ Add defendant tag
    """
    try:
        tag = Tag.objects.get(**body)
        defendant = Defendant.objects.get(id=defendant_id)

        if defendant.__class__.__name__ != tag.tagType:
            return 400, {'status': 'Bad Request', 'code': 400, 'message': 'Invalid tag for defendant'}

        for defendt in Defendant.objects.filter(customerId=defendant.customerId):

            defendt.tags.add(tag)
            defendt.save()
            for ticket in defendt.ticketDefendant.all():
                GeneralController.log_action(ticket, user, 'add tag %s' % (tag.name))

    except (KeyError, FieldError, IntegrityError, ObjectDoesNotExist, ValueError):
        return 404, {'status': 'Not Found', 'code': 404}

    code, resp = show(defendant_id)
    return code, resp


def remove_tag(defendant_id, tag_id, user):
    """ Remove defendant tag
    """
    try:
        tag = Tag.objects.get(id=tag_id)
        defendant = Defendant.objects.get(id=defendant_id)

        for defendt in Defendant.objects.filter(customerId=defendant.customerId):
            defendt.tags.remove(tag)
            defendt.save()

            for ticket in defendt.ticketDefendant.all():
                GeneralController.log_action(ticket, user, 'remove tag %s' % (tag.name))

    except (ObjectDoesNotExist, FieldError, IntegrityError, ValueError):
        return 404, {'status': 'Not Found', 'code': 404}

    code, resp = show(defendant_id)
    return code, resp


def get_or_create(defendant_id=None, customer_id=None):
    """
        Get or create defendant
        Attach previous tag if updated defendant infos
    """
    defendant_infos = {}
    if defendant_id:
        defendant_infos['id'] = defendant_id
    if customer_id:
        try:
            defendant_infos = ImplementationFactory.instance.get_singleton_of('CustomerDaoBase').get_customer_infos(customer_id)
        except CustomerDaoException:
            return None

        if not defendant_infos:
            return None
    try:
        defendant = Defendant.objects.get(**defendant_infos)
    except (TypeError, ObjectDoesNotExist):
        if not customer_id:
            return None
        defendant_infos.pop('id', None)
        tags = Defendant.objects.filter(~Q(tags=None), customerId=customer_id).values_list('tags', flat=True)
        defendant = Defendant.objects.create(**defendant_infos)
        if tags:
            defendant.tags = tags
            defendant.save()
    except MultipleObjectsReturned:
        defendant = Defendant.objects.filter(customerId=customer_id)[0]

    return defendant


def get_defendant_top20(**kwargs):
    """ Get top 20 defendant with open tickets/reports
    """
    filtr = None
    if kwargs.get('filters'):
        if kwargs['filters'] not in ['ticket', 'report']:
            return 400, {'status': 'Bad Request', 'code': 400, 'message': 'Invalid filter'}
        filtr = kwargs['filters']

    if filtr:
        res = Defendant.objects.values(
            'id', 'customerId', 'email'
        ).annotate(
            count=Count('%sDefendant' % (filtr))
        ).filter(
            ~Q(**{'%sDefendant__status' % (filtr): 'Closed'})
        ).order_by('-count')[:20]
        res = [dict(r) for r in res]
    else:
        res = {'report': [], 'ticket': []}
        for filtr in res.keys():
            res[filtr] = Defendant.objects.values(
                'id', 'customerId', 'email'
            ).annotate(
                count=Count('%sDefendant' % (filtr))
            ).filter(
                ~Q(**{'%sDefendant__status' % (filtr): 'Closed'})
            ).order_by('-count')[:20]
            res[filtr] = [dict(r) for r in res[filtr]]

    return 200, res


def get_defendant_services(customer_id):
    """
        Get services for a defendant
    """
    try:
        response = ImplementationFactory.instance.get_singleton_of('CustomerDaoBase').get_customer_services(customer_id)
    except CustomerDaoException as ex:
        return 500, {'status': 'Internal Server Error', 'code': 500, 'message': str(ex)}

    return 200, response


def get_defendant_stats(**kwargs):
    """
        Get abuse stats for a defendant
    """
    if 'defendant' in kwargs:
        customer_id = kwargs['defendant']
    else:
        return 400, {'status': 'Bad Request', 'code': 400, 'message': 'No defendant specified'}

    if 'nature' in kwargs:
        nature = kwargs['nature']
    else:
        return 400, {'status': 'Bad Request', 'code': 400, 'message': 'No type specified'}

    defendants = Defendant.objects.filter(customerId=customer_id)
    if not len(defendants):
        return 404, {'status': 'Not Found', 'code': 404}

    resp = []
    now = int(time())

    for category in Category.objects.all():
        data = {'name': category.name}
        stats = Stat.objects.filter(defendant__in=defendants, category=category.name).order_by('date')
        #  * 1000 for HighCharts
        data['data'] = [[mktime(stat.date.timetuple()) * 1000, getattr(stat, nature)] for stat in stats]
        try:
            data['data'].append([now * 1000, data['data'][-1][1]])
        except IndexError:
            pass
        resp.append(data)

    return 200, resp