"""Team workspace, milestone 1: a catalogue of every Power BI model the team keeps.

    daxlin workspace add-folder "C:/Users/me/Contoso/BI - Reports"
    daxlin workspace scan                 # nightly: reads each .pbix/.pbit, keeps a version per change
    daxlin workspace serve                # http://127.0.0.1:8766: search, usage, owners and notes
    daxlin workspace search "Total Sales"

Everything stays on the machine that runs it: one SQLite file (``pbi-workspace.db``
by default), a local web server, and the .pbix files, which are only read.
"""

from .store import Store, default_db_path  # noqa: F401
from .scan import scan_folders  # noqa: F401
