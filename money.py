"""
Microsoft Money (.mny) -> QIF conversion for banking and credit card accounts.

Money's system catalogue is encrypted, so the tables below are located by
their column signature rather than by name (see jet4.JetDatabase.find_table).
"""

import datetime

# ACCT.at values.  Only the two types below can be represented in QIF by this
# tool; everything else (investment, loan, asset, ...) is reported and skipped.
AT_BANK = 0
AT_CREDIT_CARD = 1

QIF_TYPE = {
    AT_BANK: "!Type:Bank",
    AT_CREDIT_CARD: "!Type:CCard",
}

AT_NAMES = {
    0: "bank", 1: "credit card", 2: "cash", 3: "asset", 4: "liability",
    5: "investment", 6: "loan", 7: "line of credit", 8: "mortgage",
    9: "employee stock option", 10: "other",
}

# TRN.grftt bit marking a scheduled/recurring bill template rather than a real
# register entry.  Money does not export these.
TT_SCHEDULED = 0x00200000

# TRN.cs (cleared state) -> QIF C value.
CLEARED_FLAG = {1: "*", 2: "X"}

CRLF = "\r\n"

# Written next to the QIF files, holding what QIF cannot express.  qif2kmy
# looks for this suffix in the directories it is given.
MANIFEST_SUFFIX = "_accounts.json"


class ConversionError(Exception):
    pass


def _fmt_date(d):
    """Money writes QIF dates as M/D'YYYY (no zero padding)."""
    return "%d/%d'%04d" % (d.month, d.day, d.year)


def _fmt_amount(a):
    return "{:,.2f}".format(a or 0.0)


def _sanitize(name):
    out = "".join(c if (c.isalnum() or c in " -_") else "_" for c in name)
    return "_".join(out.split()) or "account"


class Account:
    def __init__(self, row):
        self.row = row
        self.hacct = row["hacct"]
        self.name = row["szFull"] or "Account %s" % self.hacct
        self.at = row["at"]
        self.open_balance = row.get("amtOpen") or 0.0
        self.dt_open = row.get("dtOpen")
        self.dt_close = row.get("dtClose")
        self.institution = None
        self.transactions = []

    @property
    def closed(self):
        """Money sets ACCT.fClosed when an account is closed, and stamps
        ACCT.dtClose with the date.  Either one on its own still means closed.
        """
        return bool(self.row.get("fClosed")) or self.dt_close is not None

    @property
    def exportable(self):
        return self.at in QIF_TYPE

    @property
    def type_name(self):
        return AT_NAMES.get(self.at, "type %s" % self.at)

    @property
    def opening_date(self):
        """The date Money stamps on the synthetic opening-balance record.

        It is the earliest date the account has any history for: the account's
        own open date if set, otherwise (and if earlier) its first transaction.
        """
        candidates = [t.date for t in self.transactions if t.date]
        if self.dt_open:
            candidates.append(self.dt_open)
        return min(candidates) if candidates else datetime.datetime.now()


class Transaction:
    def __init__(self, row):
        self.htrn = row["htrn"]
        self.hacct = row["hacct"]
        self.hacct_link = row.get("hacctLink")
        self.date = row.get("dt")
        self.amount = row.get("amt") or 0.0
        self.hcat = row.get("hcat")
        self.hpay = row.get("lHpay")
        self.cleared = row.get("cs") or 0
        self.grftt = row.get("grftt") or 0
        self.memo = row.get("mMemo")
        self.number = row.get("szId")
        self.splits = []

    @property
    def scheduled(self):
        return bool(self.grftt & TT_SCHEDULED)


class Split:
    def __init__(self, row):
        self.htrn = row["htrn"]
        self.parent = row["htrnParent"]
        self.index = row.get("iSplit") or 0
        self.category = None
        self.memo = None
        self.amount = 0.0


class MoneyFile:
    """Reads the Money tables needed for a QIF export."""

    def __init__(self, db):
        self.db = db
        self.accounts = {}
        self.categories = {}
        self.payees = {}
        self.warnings = []
        self._load()

    # -- loading ---------------------------------------------------------

    def _table(self, label, required, forbidden=(), optional=False):
        t = self.db.find_table(required, forbidden)
        if t is None and not optional:
            raise ConversionError(
                "could not locate the %s table (no table has columns %s); "
                "this may not be a Microsoft Money file"
                % (label, ", ".join(required)))
        return t

    def _read_all(self, table, label):
        """Read a whole table, recording anything that looks like data loss."""
        rows = list(table.rows())
        if table.read_errors:
            self.warnings.append("%s: %d row(s) could not be decoded"
                                 % (label, table.read_errors))
        if len(rows) < table.num_rows:
            self.warnings.append("%s: read %d of %d row(s) recorded in the file"
                                 % (label, len(rows), table.num_rows))
        return rows

    def _load(self):
        acct_t = self._table("account (ACCT)", ["hacct", "szFull", "at"])
        trn_t = self._table("transaction (TRN)",
                            ["htrn", "hacct", "dt", "amt", "hcat", "cs", "grftt"])
        cat_t = self._table("category (CAT)", ["hcat", "szFull", "hcatParent"])
        pay_t = self._table("payee (PAY)", ["hpay", "szFull", "hpayParent"],
                            optional=True)
        split_t = self._table("split (TRN_SPLIT)", ["htrn", "htrnParent", "iSplit"],
                              optional=True)
        bill_t = self._table("recurring bill (BILL)",
                             ["hbill", "frq", "iinstNextUnpaid"], optional=True)

        institutions = {}
        fi_t = self._table("institution (FI)", ["hfi", "szFull", "haddr", "fHidden"],
                           optional=True)
        if fi_t:
            institutions = {r["hfi"]: r["szFull"]
                            for r in self._read_all(fi_t, "institutions")
                            if r["hfi"] is not None and r["szFull"]}

        for r in self._read_all(acct_t, "accounts"):
            if r["hacct"] is not None:
                acct = Account(r)
                acct.institution = institutions.get(r.get("hfi"))
                self.accounts[r["hacct"]] = acct

        self._cat_rows = {r["hcat"]: r
                          for r in self._read_all(cat_t, "categories")
                          if r["hcat"] is not None}
        if pay_t:
            self.payees = {r["hpay"]: r["szFull"]
                           for r in self._read_all(pay_t, "payees")
                           if r["hpay"] is not None}

        # Transactions referenced by a scheduled bill are templates, not
        # register entries.  Money excludes them and so do we.
        scheduled = set()
        if bill_t:
            for r in bill_t.rows():
                if r.get("lHtrn") not in (None, -1):
                    scheduled.add(r["lHtrn"])
        xfer_t = self._table("transfer (TRN_XFER)", ["htrnFrom", "htrnLink"],
                             optional=True)
        if xfer_t and scheduled:
            for r in xfer_t.rows():
                a, b = r.get("htrnFrom"), r.get("htrnLink")
                if a in scheduled or b in scheduled:
                    scheduled.update(x for x in (a, b) if x is not None)

        self.transactions = {}
        for r in self._read_all(trn_t, "transactions"):
            if r["htrn"] is None:
                continue
            t = Transaction(r)
            # A row with no account cannot appear in any register.
            if t.hacct is None or t.scheduled or t.htrn in scheduled:
                continue
            self.transactions[t.htrn] = t

        if split_t:
            self._attach_splits(split_t)

        for t in self.transactions.values():
            acct = self.accounts.get(t.hacct)
            if acct is not None:
                acct.transactions.append(t)
        for acct in self.accounts.values():
            acct.transactions.sort(key=lambda t: (t.date or datetime.datetime.min,
                                                  t.htrn))

    def _attach_splits(self, split_t):
        """Money stores split lines as child TRN rows linked via TRN_SPLIT."""
        for r in split_t.rows():
            parent = self.transactions.get(r.get("htrnParent"))
            child = self.transactions.pop(r.get("htrn"), None)
            if parent is None or child is None:
                continue
            s = Split(r)
            s.amount = child.amount
            s.memo = child.memo
            s.category = self.transfer_or_category(child)
            parent.splits.append(s)
        for t in self.transactions.values():
            t.splits.sort(key=lambda s: s.index)

    # -- lookups ---------------------------------------------------------

    def category_path(self, hcat):
        """Full 'Parent:Child' category name, excluding the INCOME/EXPENSE root."""
        if hcat in (None, -1):
            return None
        if hcat in self.categories:
            return self.categories[hcat]
        parts = []
        seen = set()
        cur = hcat
        while cur not in (None, -1) and cur not in seen:
            seen.add(cur)
            row = self._cat_rows.get(cur)
            if row is None:
                break
            parts.append(row["szFull"] or "")
            cur = row.get("hcatParent")
        if len(parts) > 1:
            parts.pop()  # drop the top-level INCOME / EXPENSE root
        path = ":".join(reversed([p for p in parts if p]))
        self.categories[hcat] = path or None
        return self.categories[hcat]

    def payee_name(self, hpay):
        if hpay in (None, -1):
            return None
        return self.payees.get(hpay)

    def transfer_or_category(self, t):
        """QIF 'L' value: a bracketed account for transfers, else the category."""
        if t.hacct_link not in (None, -1):
            other = self.accounts.get(t.hacct_link)
            if other is not None:
                return "[%s]" % other.name
        return self.category_path(t.hcat)

    # -- QIF output ------------------------------------------------------

    def account_qif(self, acct):
        """Render one account as a complete QIF document."""
        lines = [QIF_TYPE[acct.at]]

        # Money leads with a synthetic opening-balance record, modelled as a
        # reconciled transfer from the account to itself.
        lines += ["D" + _fmt_date(acct.opening_date),
                  "T" + _fmt_amount(acct.open_balance),
                  "CX",
                  "POpening Balance",
                  "L[%s]" % acct.name,
                  "^"]

        for t in acct.transactions:
            lines.append("D" + _fmt_date(t.date) if t.date else "D")
            lines.append("T" + _fmt_amount(t.amount))
            flag = CLEARED_FLAG.get(t.cleared)
            if flag:
                lines.append("C" + flag)
            if t.number:
                lines.append("N" + t.number)
            payee = self.payee_name(t.hpay)
            if payee:
                lines.append("P" + payee)
            if t.memo:
                lines.append("M" + t.memo)
            cat = self.transfer_or_category(t)
            if cat:
                lines.append("L" + cat)
            for s in t.splits:
                lines.append("S" + (s.category or ""))
                if s.memo:
                    lines.append("E" + s.memo)
                lines.append("$" + _fmt_amount(s.amount))
            lines.append("^")

        return CRLF.join(lines) + CRLF

    def exportable_accounts(self):
        return [a for a in sorted(self.accounts.values(), key=lambda a: a.hacct)
                if a.exportable]

    def skipped_accounts(self):
        return [a for a in sorted(self.accounts.values(), key=lambda a: a.hacct)
                if not a.exportable]

    @staticmethod
    def filename_for(acct, stem):
        return "%s_%s.qif" % (stem, _sanitize(acct.name))

    @staticmethod
    def manifest_name(stem):
        return stem + MANIFEST_SUFFIX

    def manifest(self, stem, source):
        """The account facts QIF has no field for, for qif2kmy to pick up.

        QIF describes a register, not an account, so there is nowhere to say
        that Money had an account closed.  Writing it alongside the QIF files
        keeps them byte-for-byte what Money itself would have exported.

        Every exportable account is listed, including ones --skip-closed left
        out, so that an omitted account still reached by a transfer can be
        marked closed rather than quietly reopened.
        """
        return {
            "generator": "mny2qif",
            "source": source,
            "accounts": [
                {
                    "name": a.name,
                    "qif": self.filename_for(a, stem),
                    "closed": a.closed,
                    "institution": a.institution,
                }
                for a in self.exportable_accounts()
            ],
        }
