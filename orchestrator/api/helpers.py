# Copyright 2019-2020 SURF.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from shlex import shlex
from typing import Any, Generator, List, Optional, Union
from uuid import UUID

from more_itertools import chunked, first
from sqlalchemy import String, cast, func
from sqlalchemy.orm import Query
from sqlalchemy.sql import expression
from starlette.responses import Response
from structlog import get_logger

from orchestrator.api.error_handling import raise_status
from orchestrator.db import ProcessTable, ProductTable, SubscriptionTable
from orchestrator.db.models import SubscriptionSearchView
from orchestrator.domain.base import SubscriptionModel

logger = get_logger(__name__)


def _quote_if_kv_pair(token: str) -> str:
    return f'"{token}"' if ":" in token else token


def _process_text_query(q: str) -> str:
    quote = '"'
    if q.count(quote) % 2 == 1:
        q += quote  # Add missing closing quote
    lex = shlex(q)
    lex.whitespace_split = True
    lex.quotes = '"'
    try:
        return " ".join([_quote_if_kv_pair(token) for token in lex])
    except ValueError:
        logger.debug("Error parsing text query.")
        return q


def _query_with_filters(
    response: Response,
    query: Query,
    range: Optional[List[int]] = None,
    sort: Optional[List[str]] = None,
    filters: Optional[List[str]] = None,
) -> List:
    if filters is not None:
        for filter in chunked(filters, 2):
            if filter and len(filter) == 2:
                field = filter[0]
                value = filter[1]
                value_as_bool = value.lower() in ("yes", "y", "ye", "true", "1", "ja", "insync")
                if value is not None:
                    if field.endswith("_gt"):
                        query = query.filter(SubscriptionTable.__dict__[field[:-3]] > value)
                    elif field.endswith("_gte"):
                        query = query.filter(SubscriptionTable.__dict__[field[:-4]] >= value)
                    elif field.endswith("_lte"):
                        query = query.filter(SubscriptionTable.__dict__[field[:-4]] <= value)
                    elif field.endswith("_lt"):
                        query = query.filter(SubscriptionTable.__dict__[field[:-3]] < value)
                    elif field.endswith("_ne"):
                        query = query.filter(SubscriptionTable.__dict__[field[:-3]] != value)
                    elif field == "insync":
                        query = query.filter(SubscriptionTable.insync.is_(value_as_bool))
                    elif field == "tags":
                        # For node and port selector form widgets
                        sub_values = value.split("-")
                        query = query.filter(func.lower(ProductTable.tag).in_([s.lower() for s in sub_values]))
                    elif field == "tag":
                        # For React table 7
                        sub_values = value.split("-")
                        query = query.filter(func.lower(ProductTable.tag).in_([s.lower() for s in sub_values]))
                    elif field == "product":
                        sub_values = value.split("-")
                        query = query.filter(func.lower(ProductTable.name).in_([s.lower() for s in sub_values]))
                    elif field == "status":
                        # For React table 7
                        statuses = value.split("-")
                        query = query.filter(SubscriptionTable.status.in_([s.lower() for s in statuses]))
                    elif field == "statuses":
                        # For port subscriptions
                        sub_values = value.split("-")
                        query = query.filter(SubscriptionTable.status.in_([s.lower() for s in sub_values]))
                    elif field == "organisation":
                        try:
                            value_as_uuid = UUID(value)
                        except (ValueError, AttributeError):
                            msg = "Not a valid customer_id, must be a UUID: '{value}'"
                            logger.debug(msg)
                            raise_status(HTTPStatus.BAD_REQUEST, msg)
                        query = query.filter(SubscriptionTable.customer_id == value_as_uuid)
                    elif field == "tsv":
                        # Quote key:value tokens. This will use the FOLLOWED BY operator (https://www.postgresql.org/docs/13/textsearch-controls.html)
                        processed_text_query = _process_text_query(value)

                        logger.debug("Running full-text search query:", value=processed_text_query)
                        # TODO: Make 'websearch_to_tsquery' into a sqlalchemy extension
                        query = query.join(SubscriptionSearchView).filter(
                            func.websearch_to_tsquery("simple", processed_text_query).op("@@")(
                                SubscriptionSearchView.tsv
                            )
                        )

                    elif field in SubscriptionTable.__dict__:
                        query = query.filter(cast(SubscriptionTable.__dict__[field], String).ilike("%" + value + "%"))

    if sort is not None and len(sort) >= 2:
        for item in chunked(sort, 2):
            if item and len(item) == 2:
                if item[0] in ["product", "tag"]:
                    field = "name" if item[0] == "product" else "tag"
                    if item[1].upper() == "DESC":
                        query = query.order_by(expression.desc(ProductTable.__dict__[field]))
                    else:
                        query = query.order_by(expression.asc(ProductTable.__dict__[field]))
                else:
                    if item[1].upper() == "DESC":
                        query = query.order_by(expression.desc(SubscriptionTable.__dict__[item[0]]))
                    else:
                        query = query.order_by(expression.asc(SubscriptionTable.__dict__[item[0]]))

    if range is not None and len(range) == 2:
        try:
            range_start = int(range[0])
            range_end = int(range[1])
            if range_start >= range_end:
                raise ValueError("range start must be lower than end")
        except (ValueError, AssertionError):
            msg = "Invalid range parameters"
            logger.exception(msg)
            raise_status(HTTPStatus.BAD_REQUEST, msg)
        total = query.count()
        query = query.slice(range_start, range_end)

        response.headers["Content-Range"] = f"subscriptions {range_start}-{range_end}/{total}"

    return query.all()


VALID_SORT_KEYS = {
    "creator": "created_by",
    "started": "started_at",
    "status": "last_status",
    "assignee": "assignee",
    "modified": "last_modified_at",
    "workflow": "workflow",
}


@dataclass
class ProductEnriched:
    product_id: UUID
    description: str
    name: str
    tag: str
    status: str
    product_type: str


@dataclass
class _Subscription:
    customer_id: UUID
    description: str
    end_date: float
    insync: bool
    start_date: float
    status: str
    subscription_id: UUID
    product: ProductEnriched


@dataclass
class _ProcessListItem:
    assignee: str
    created_by: Optional[str]
    failed_reason: Optional[str]
    last_modified_at: datetime
    pid: UUID
    started_at: datetime
    last_status: str
    last_step: Optional[str]
    subscriptions: List[_Subscription]
    workflow: str
    workflow_target: Optional[str]
    is_task: bool


def enrich_process(p: ProcessTable) -> _ProcessListItem:
    # p.subscriptions is a non JSON serializable AssociationProxy
    # So we need to build a list of Subscriptions here.
    subscriptions: List[_Subscription] = []
    for sub in p.subscriptions:
        prod = sub.product

        subscription = _Subscription(
            customer_id=sub.customer_id,
            description=sub.description,
            end_date=sub.end_date if sub.end_date else None,
            insync=sub.insync,
            start_date=sub.start_date if sub.start_date else None,
            status=sub.status,
            subscription_id=sub.subscription_id,
            product=ProductEnriched(
                product_id=prod.product_id,
                description=prod.description,
                name=prod.name,
                tag=prod.tag,
                status=prod.status,
                product_type=prod.product_type,
            ),
        )
        subscriptions.append(subscription)

    return _ProcessListItem(
        assignee=p.assignee,
        created_by=p.created_by,
        failed_reason=p.failed_reason,
        last_modified_at=p.last_modified_at,
        pid=p.pid,
        started_at=p.started_at.timestamp(),
        last_status=p.last_status,
        last_step=p.last_step,
        subscriptions=subscriptions,
        workflow=p.workflow,
        workflow_target=first([ps.workflow_target for ps in p.process_subscriptions], None),
        is_task=p.is_task,
    )


def update_in(dct: Union[dict, list], path: str, value: Any, sep: str = ".") -> None:
    """Update a value in a dict or list based on a path."""
    for x in path.split(sep):
        prev: Union[dict, list]
        if x.isdigit() and isinstance(dct, list):
            prev = dct
            dct = dct[int(x)]
        else:
            prev = dct
            dct = dict(dct).setdefault(x, {})
    prev[x] = value  # type: ignore


def get_in(dct: Union[dict, list], path: str, sep: str = ".") -> Any:
    """Get a value in a dict or list using the path and get the resulting key's value."""
    prev: Union[dict, list]
    for x in path.split(sep):
        if x.isdigit() and isinstance(dct, list):
            prev, dct = dct, dct[int(x)]
        else:
            prev, dct = dct, dict(dct).get(x)  # type: ignore
    return prev[x]  # type: ignore


def getattr_in(obj: Any, attr: str) -> Any:
    """Get an instance attribute value by path."""

    def _getattr(obj: object, attr: str) -> Any:
        if isinstance(obj, list):
            return obj[int(attr)]

        if isinstance(obj, dict):
            return obj.get(attr)

        return getattr(obj, attr, None)

    return functools.reduce(_getattr, [obj] + attr.split("."))


def product_block_paths(subscription: Union[SubscriptionModel, dict]) -> List[str]:
    _subscription = subscription.dict() if isinstance(subscription, SubscriptionModel) else subscription

    def get_dict_items(d: dict) -> Generator:
        for k, v in d.items():
            if isinstance(v, dict):
                for k1, v1 in get_dict_items(v):
                    yield (f"{k}.{k1}", v1)
                yield (k, v)
            if isinstance(v, list):
                for index, list_item in enumerate(v):
                    if isinstance(list_item, dict):
                        for list_item_key, list_item_value in get_dict_items(list_item):
                            yield (f"{k}.{index}.{list_item_key}", list_item_value)
                        yield (f"{k}.{index}", list_item)

    return [path for path, value in get_dict_items(_subscription)]
