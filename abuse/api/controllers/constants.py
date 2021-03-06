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
    Cerberus Api constants
"""

import re

from ...models import AttachedDocument, Category, Report, Ticket

# GeneralController
#

GENERAL_DASHBOARD_STATUS = {
    "idle": ("Open", "Reopened"),
    "waiting": ("WaitingAnswer", "Paused"),
    "pending": ("Answered", "Alarm"),
}

GENERAL_CHECK_PERM_DEFENDANT_LEVEL = ("Beginner", "Advanced", "Expert")
GENERAL_MASS_CONTACT_REQUIRED = ("{{ service }}", "{{ publicId }}", "{% if lang ==")

GENERAL_SEARCH_EXTRA_FIELDS = [
    "defendantTag",
    "providerTag",
    "defendant",
    "defendantCountry",
    "providerEmail",
    "item",
    "fulltext",
]
GENERAL_SEARCH_REPORT_FIELDS = list(
    set(
        [f.name for f in Report._meta.fields]
        + GENERAL_SEARCH_EXTRA_FIELDS
        + ["reportTag"]
    )
)
GENERAL_SEARCH_TICKET_FIELDS = list(
    set(
        [f.name for f in Ticket._meta.fields]
        + GENERAL_SEARCH_EXTRA_FIELDS
        + ["ticketTag", "attachedReportsCount", "ticketIds", "starredBy"]
    )
)

GENERAL_SEARCH_MAPPING = {
    "defendant": ["defendantEmail", "defendantCustomerId"],
    "item": ["itemFqdnResolved", "itemIpReverse", "itemRawItem"],
    "ticketIds": ["id", "publicId"],
}

GENERAL_TOOLBAR_TODO_STATUS = ("ActionError", "Alarm", "Open", "Reopened")

GENERAL_TOOLBAR_SLEEPING_STATUS = ("Paused", "WaitingAnswer")

GENERAL_TOOLBAR_ALL_STATUS = (
    "ActionError",
    "Answered",
    "Alarm",
    "Reopened",
    "Open",
    "Paused",
    "WaitingAnswer",
)

# TicketsController
#

TICKET_FIELDS = [fld.name for fld in Ticket._meta.fields]
TICKET_STATUS = [status[0].lower() for status in Ticket.TICKET_STATUS]

# Mapping JSON fields name to django syntax
TICKET_FILTER_MAPPING = (
    ("ticketsTag", "tags__name"),
    ("reportsTag", "reportTicket__tags__name"),
    ("treatedBy", "treatedBy__username"),
    ("starredBy", "starredBy__user__username"),
    ("defendantCustomerId", "defendant__customerId"),
    ("defendantCountry", "defendant__details__country"),
    ("defendantEmail", "defendant__details__email"),
    ("defendantTag", "defendant__tags__name"),
    ("providerEmail", "reportTicket__provider__email"),
    ("providerTag", "reportTicket__provider__tags__name"),
    ("itemRawItem", "reportTicket__reportItemRelatedReport__rawItem"),
    ("itemIpReverse", "reportTicket__reportItemRelatedReport__ipReverse"),
    ("itemFqdnResolved", "reportTicket__reportItemRelatedReport__fqdnResolved"),
)

TICKET_UPDATE_VALID_FIELDS = (
    "defendant",
    "category",
    "level",
    "alarm",
    "treatedBy",
    "confidential",
    "priority",
    "pauseStart",
    "pauseDuration",
    "moderation",
    "protected",
    "escalated",
    "update",
)

TICKET_BULK_VALID_FIELDS = (
    "category",
    "level",
    "alarm",
    "treatedBy",
    "confidential",
    "priority",
    "moderation",
    "protected",
    "escalated",
    "update",
    "pauseDuration",
)

TICKET_BULK_VALID_STATUS = ("unpaused", "paused", "closed", "reopened")

TICKET_MODIFICATION_INVALID_FIELDS = (
    "defendant",
    "category",
    "treatedBy",
    "snoozeStart",
    "creationDate",
    "modificationDate",
)

# ReportsController
#

REPORT_STATUS = [status[0].lower() for status in Report.REPORT_STATUS]

# Mapping JSON fields name to django syntax
REPORT_FILTER_MAPPING = (
    ("reportTag", "tags__name"),
    ("providerEmail", "provider__email"),
    ("providerTag", "provider__tags__name"),
    ("defendantCustomerId", "defendant__customerId"),
    ("defendantCountry", "defendant__details__country"),
    ("defendantEmail", "defendant__details__email"),
    ("defendantTag", "defendant__tags__name"),
    ("itemRawItem", "reportItemRelatedReport__rawItem"),
    ("itemIpReverse", "reportItemRelatedReport__ipReverse"),
    ("itemFqdnResolved", "reportItemRelatedReport__fqdnResolved"),
)

REPORT_ATTACHMENT_FIELDS = [fld.name for fld in AttachedDocument._meta.fields]
REPORT_FIELDS = [fld.name for fld in Report._meta.fields]

# MISC
#

IP_CIDR_RE = re.compile(
    r"(?<!\d\.)(?<!\d)(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}(?!\d|(?:\.\d))"
)
ALL_CATEGORIES = set(list(Category.all().values_list("name", flat=True)))
