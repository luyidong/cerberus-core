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
    Parsing template for abuseteam@cloudflare.com
"""

from abuse.parsers import Parser

TEMPLATE = {
    "email": "abuseteam@cloudflare.com",
    "regexp": {
        "ips": {"pattern": r"(?:currently\s*resolves\s*to\s*)" + Parser.ipv4_re},
        "fqdn": {
            "pattern": r"(?:the\s*domain\s*)("
            + Parser.domain_re
            + r")(?:\s*currently\s*resolves\s*to)"
        },
        "category": {"pattern": r"((.|\n|\r|\t)*)", "transform": True},
    },
}
