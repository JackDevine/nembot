"""How nemsplainer credits AEMO, its data source.

AEMO's Copyright Permissions allow use of AEMO material for any purpose with
accurate attribution to AEMO as author. Some older AEMO documents are stricter
(see plan.md, "Licensing"), so this text also disclaims endorsement and warranty.
"""

COPYRIGHT_URL = 'https://www.aemo.com.au/privacy-and-legal-notices/copyright-permissions'

CREDIT = ('Contains data from the Australian Energy Market Operator (AEMO), published on NEMWEB, used under '
          f"AEMO's Copyright Permissions ({COPYRIGHT_URL.removeprefix('https://www.')}). Processed by nemsplainer; "
          'not endorsed by AEMO. AEMO gives no warranty of accuracy, completeness or currency; use at your own risk.')
