# -*- coding: utf-8 -*-
"""
Lokálna chybová trieda – nahrádza tools_archivczsk.contentprovider.exception.
"""


class AddonErrorException(Exception):
    """Generická chyba pluginu – zobrazí sa užívateľovi v dialógu / notifikácii."""
    pass
