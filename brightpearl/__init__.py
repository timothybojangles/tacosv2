"""Modular Brightpearl API helpers.

This package exposes focused modules that mirror the GUI tool sections.
Import call sites should prefer the dedicated submodules (e.g. 
``from brightpearl.inventory_import import update_product_catalogue``) so
that new features can be added without editing existing modules.
"""

from .inventory_import import update_product_catalogue
from .additional_addresses import update_contact_catalogue
from .forget_contact import update_forget_contacts_catalogue
from .warehouse_locations import update_location_catalogue

__all__ = [
    "update_product_catalogue",
    "update_contact_catalogue",
    "update_forget_contacts_catalogue",
    "update_location_catalogue",
]
