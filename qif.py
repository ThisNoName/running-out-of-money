"""
QIF parsing, shaped around the files mny2qif writes.

One QIF file holds one account.  The account's own name is not in a header:
Money identifies it with the synthetic leading "Opening Balance" record, which
is categorised as a transfer to the account itself (see the mny2qif README), so
that record is what names the file's account here too.
"""

import datetime
import os
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction

# !Type: header -> the kind of account the file describes.
FILE_TYPES = {
    "bank": "bank",
    "ccard": "ccard",
    "cash": "cash",
    "oth a": "asset",
    "oth l": "liability",
}

# !Type: sections that are lists rather than registers, and carry no entries.
LIST_TYPES = ("cat", "payee", "memorized", "prices", "security", "class",
              "invitem", "template", "budget", "invst", "invoice")

# QIF C values -> KMyMoney reconcileflag.  A bare "C" means cleared.
CLEARED = {"": 1, "*": 1, "c": 1, "x": 2, "r": 2}

OPENING_BALANCE_PAYEE = "opening balance"

# M/D/Y with any non-digit separators, which covers Money's M/D'YYYY as well as
# the more common M/D/YYYY.  Quicken also pads single digits with a space.
_DATE_RE = re.compile(r"^(\d{1,2})\D{1,2}(\d{1,2})\D{1,2}(\d{2,4})$")


class QifError(Exception):
    pass


def parse_date(text):
    """Parse a QIF D value into "YYYY-MM-DD".

    Month comes first; Money writes no other order.  ISO strings are what the
    KMyMoney file wants and they sort chronologically, so no date object is
    kept beyond validation.
    """
    m = _DATE_RE.match(text.replace(" ", ""))
    if not m:
        raise QifError("unrecognised date %r" % text)
    month, day, year = (int(g) for g in m.groups())
    if year < 100:
        year += 2000 if year < 70 else 1900
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError as exc:
        raise QifError("bad date %r: %s" % (text, exc))


def parse_amount(text):
    """Parse a QIF T/U/$ value into an exact Fraction."""
    s = text.strip().replace(",", "").replace("$", "").lstrip("+")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if not s or s == "-":
        return Fraction(0)
    try:
        return Fraction(Decimal(s))
    except (InvalidOperation, ValueError):
        raise QifError("unrecognised amount %r" % text)


def split_category(text):
    """Split an L/S value into (transfer_account, category).  Only one is set.

    Transfers are bracketed.  A trailing "/Class" is dropped; KMyMoney has no
    equivalent for Quicken classes.
    """
    value = text.strip()
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            return value[1:end].strip(), None
        return value[1:].strip(), None
    value = value.split("/", 1)[0].strip()
    return None, value or None


class Split:
    """One S/E/$ group inside a split transaction."""

    def __init__(self):
        self.transfer = None
        self.category = None
        self.memo = ""
        self.amount = Fraction(0)


class Entry:
    """One QIF record, terminated by a '^' line."""

    def __init__(self, source, lineno):
        self.source = source        # file the record came from, for messages
        self.lineno = lineno
        self.date = None            # "YYYY-MM-DD"
        self.amount = Fraction(0)
        self.reconcileflag = 0
        self.number = ""
        self.payee = ""
        self.memo = ""
        self.transfer = None        # other account name, for a transfer
        self.category = None        # category path, for a categorised entry
        self.splits = []

    @property
    def where(self):
        return "%s:%d" % (os.path.basename(self.source), self.lineno)


class QifAccount:
    """A parsed QIF file: one account and its register."""

    def __init__(self, path, kind):
        self.path = path
        self.kind = kind
        self.name = None
        self.opening_date = None
        self.opening_balance = Fraction(0)
        self.opening_reconcileflag = 2
        self.entries = []


def _parse_entries(path):
    """Yield (header_kind, [Entry, ...]) for the register sections of a file."""
    kind = None
    entries = []
    entry = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue

            if line.startswith("!"):
                tag = line[1:].strip().lower()
                if tag.startswith("type:"):
                    name = tag[5:].strip()
                    if name in LIST_TYPES:
                        kind = None         # skip until the next register
                    elif name in FILE_TYPES:
                        kind = FILE_TYPES[name]
                    else:
                        raise QifError("%s: unsupported section !Type:%s"
                                       % (os.path.basename(path), name))
                # !Account, !Clear:AutoSwitch and friends carry no entries we
                # need; the opening-balance record names the account instead.
                continue

            if kind is None:
                continue

            if line.startswith("^"):
                if entry is not None:
                    entries.append(entry)
                    entry = None
                continue

            if entry is None:
                entry = Entry(path, lineno)

            code, value = line[0], line[1:]
            if code == "D":
                entry.date = parse_date(value)
            elif code in ("T", "U"):
                entry.amount = parse_amount(value)
            elif code == "C":
                flag = CLEARED.get(value.strip().lower())
                if flag is None:
                    raise QifError("%s:%d: unrecognised C value %r"
                                   % (os.path.basename(path), lineno, value))
                entry.reconcileflag = flag
            elif code == "N":
                entry.number = value.strip()
            elif code == "P":
                entry.payee = value.strip()
            elif code == "M":
                entry.memo = value
            elif code == "L":
                entry.transfer, entry.category = split_category(value)
            elif code == "S":
                entry.splits.append(Split())
                entry.splits[-1].transfer, entry.splits[-1].category = \
                    split_category(value)
            elif code == "E":
                if entry.splits:
                    entry.splits[-1].memo = value
            elif code == "$":
                if entry.splits:
                    entry.splits[-1].amount = parse_amount(value)
            # A (address), F (reimbursable) and investment codes are ignored.

    if entry is not None:            # a final record with no '^'
        entries.append(entry)
    return kind, entries


def load(path):
    """Parse one QIF file into a QifAccount.

    The leading "Opening Balance" self-transfer is consumed: it names the
    account and supplies its opening date and balance rather than becoming a
    transaction of its own.
    """
    kind, entries = _parse_entries(path)
    if kind is None:
        raise QifError("%s: no register section (!Type:Bank, !Type:CCard, ...)"
                       % os.path.basename(path))

    account = QifAccount(path, kind)

    opening = None
    for entry in entries:
        if (entry.payee.strip().lower() == OPENING_BALANCE_PAYEE
                and entry.transfer and not entry.splits):
            opening = entry
            break

    if opening is not None:
        account.name = opening.transfer
        account.opening_date = opening.date
        account.opening_balance = opening.amount
        account.opening_reconcileflag = opening.reconcileflag
        entries.remove(opening)
    else:
        # No Money-style opening record: fall back to the file name and let the
        # first transaction date stand in as the opening date.
        account.name = os.path.splitext(os.path.basename(path))[0]
        dated = [e.date for e in entries if e.date]
        account.opening_date = min(dated) if dated else None

    account.entries = entries
    return account
