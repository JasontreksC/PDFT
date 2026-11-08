import threading

# MuPDF's Python binding shares one global context, so every PyMuPDF call that
# touches a document/font is serialised through this lock. Long jobs take it per
# page so page rendering requests can interleave.
PYMU_LOCK = threading.RLock()
