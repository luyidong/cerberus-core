# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2017, OVH SAS
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
    News views for Cerberus protected API.
"""

from flask import Blueprint, g, request

from ..decorators import admin_required
from ..controllers import news as NewsController

news_views = Blueprint("news_views", __name__, url_prefix="/news")


@news_views.route("", methods=["GET"])
def get_all_news():
    """
        Get news
    """
    return NewsController.get_news(filters=request.args.get("filters"))


@news_views.route("/<news>", methods=["GET"])
def get_news(news=None):
    """ Get given news
    """
    return NewsController.show(news)


@news_views.route("", methods=["POST"])
def create_news():
    """ Post a news
    """
    body = request.get_json()
    return NewsController.create(body, g.user)


@news_views.route("/<news>", methods=["PUT"])
def update_news(news=None):
    """ Update given news
    """
    body = request.get_json()
    return NewsController.update(news, body, g.user)


@news_views.route("/<news>", methods=["DELETE"])
@admin_required
def delete_news(news=None):
    """ Delete given news
    """
    return NewsController.destroy(news)
