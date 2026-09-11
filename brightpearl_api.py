"""Deprecated compatibility layer.

Prefer importing from the ``brightpearl`` package directly, e.g.::

    from brightpearl.inventory_import import update_product_catalogue

This module remains for legacy callers that still expect the old
``brightpearl_api`` module.
"""

from brightpearl.inventory_import import update_product_catalogue
from brightpearl.additional_addresses import update_contact_catalogue
from brightpearl.forget_contact import update_forget_contacts_catalogue
from brightpearl.warehouse_locations import update_location_catalogue

__all__ = [
    "update_product_catalogue",
    "update_contact_catalogue",
    "update_forget_contacts_catalogue",
    "update_location_catalogue",
]
