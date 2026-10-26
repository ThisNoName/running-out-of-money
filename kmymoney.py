"""
A minimal KMyMoney book model and XML writer.

Only what a QIF import needs is modelled: payees, a chart of accounts rooted at
the five standard accounts, and double-entry transactions.  Investments,
schedules, budgets and reports are written as empty sections.

KMyMoney stores a .kmy as gzipped XML; see write().
"""

import gzip
import xml.etree.ElementTree as ET
from fractions import Fraction

# KMyMoney MyMoneyAccount::accountTypeE values.
CHECKING = 1
SAVINGS = 2
CASH = 3
CREDIT_CARD = 4
ASSET = 9
LIABILITY = 10
INCOME = 12
EXPENSE = 13
EQUITY = 16

# Standard (root) accounts, in the order KMyMoney writes them.
STD_ASSET = "AStd::Asset"
STD_LIABILITY = "AStd::Liability"
STD_EXPENSE = "AStd::Expense"
STD_INCOME = "AStd::Income"
STD_EQUITY = "AStd::Equity"

STANDARD_ACCOUNTS = [
    (STD_ASSET, "Asset", ASSET),
    (STD_LIABILITY, "Liability", LIABILITY),
    (STD_EXPENSE, "Expense", EXPENSE),
    (STD_INCOME, "Income", INCOME),
    (STD_EQUITY, "Equity", EQUITY),
]

# KMyMoney's own default for category accounts, which have no real open date.
CATEGORY_OPENED = "1900-01-01"

# An account is closed by a key/value pair on it, not by an attribute.
CLOSED_KEY = "mm-closed"
CLOSED_VALUE = "yes"

XML_HEADER = ('<?xml version="1.0" encoding="utf-8"?>\n'
              '<!DOCTYPE KMYMONEY-FILE>\n')


def rational(value):
    """Format an amount the way KMyMoney does: a reduced fraction, "n/d"."""
    f = Fraction(value)
    return "%d/%d" % (f.numerator, f.denominator)


class Account:
    def __init__(self, ident, name, type_, parent, opened="", description=""):
        self.id = ident
        self.name = name
        self.type = type_
        self.parent = parent
        self.opened = opened
        self.description = description
        self.children = []
        self.pairs = {}
        self.institution = ""
        self.lastmodified = ""


class Split:
    def __init__(self, account, value, payee="", reconcileflag=0, memo="",
                 number="", bankid=""):
        self.account = account
        self.value = Fraction(value)
        self.payee = payee
        self.reconcileflag = reconcileflag
        self.memo = memo
        self.number = number
        self.bankid = bankid


class Institution:
    def __init__(self, ident, name):
        self.id = ident
        self.name = name
        self.accounts = []


class Transaction:
    def __init__(self, ident, postdate, splits):
        self.id = ident
        self.postdate = postdate
        self.splits = splits


class Book:
    def __init__(self, currency="USD", entrydate=None, file_id=None):
        self.currency = currency
        self.entrydate = entrydate
        self.file_id = file_id
        self.accounts = {}
        self.order = []
        self.payees = {}
        self.payee_order = []
        self.transactions = []
        self.institutions = {}
        self.institution_order = []
        self._next_institution = 0
        self._next_account = 0
        self._next_payee = 0
        self._next_transaction = 0

        for ident, name, type_ in STANDARD_ACCOUNTS:
            self.accounts[ident] = Account(ident, name, type_, "")

    # -- accounts --------------------------------------------------------

    def add_account(self, name, type_, parent, opened="", description=""):
        self._next_account += 1
        ident = "A%06d" % self._next_account
        account = Account(ident, name, type_, parent, opened, description)
        account.lastmodified = self.entrydate or ""
        self.accounts[ident] = account
        self.order.append(ident)
        self.accounts[parent].children.append(ident)
        return ident

    def child_named(self, parent, name):
        for ident in self.accounts[parent].children:
            if self.accounts[ident].name == name:
                return ident
        return None

    def category(self, path, type_):
        """Resolve a "Parent:Child" category path, creating what is missing."""
        parent = STD_INCOME if type_ == INCOME else STD_EXPENSE
        for part in [p.strip() for p in path.split(":") if p.strip()]:
            existing = self.child_named(parent, part)
            if existing is None:
                existing = self.add_account(part, type_, parent,
                                            opened=CATEGORY_OPENED)
            parent = existing
        return parent

    # -- institutions ----------------------------------------------------

    def set_institution(self, account_id, name):
        """Attach an account to the institution of that name, creating it."""
        for ident in self.institution_order:
            if self.institutions[ident].name == name:
                break
        else:
            self._next_institution += 1
            ident = "I%06d" % self._next_institution
            self.institutions[ident] = Institution(ident, name)
            self.institution_order.append(ident)
        self.institutions[ident].accounts.append(account_id)
        self.accounts[account_id].institution = ident

    # -- payees ----------------------------------------------------------

    def payee(self, name):
        if not name:
            return ""
        if name not in self.payees:
            self._next_payee += 1
            self.payees[name] = "P%06d" % self._next_payee
            self.payee_order.append(name)
        return self.payees[name]

    # -- transactions ----------------------------------------------------

    def add_transaction(self, postdate, splits):
        self._next_transaction += 1
        ident = "T%018d" % self._next_transaction
        transaction = Transaction(ident, postdate, splits)
        self.transactions.append(transaction)
        return transaction

    # -- XML -------------------------------------------------------------

    def _account_element(self, parent_el, account):
        el = ET.SubElement(parent_el, "ACCOUNT", {
            "institution": account.institution,
            "parentaccount": account.parent,
            "lastmodified": account.lastmodified,
            "type": str(account.type),
            "number": "",
            "currency": self.currency,
            "lastreconciled": "",
            "description": account.description,
            "id": account.id,
            "name": account.name,
            "opened": account.opened,
        })
        if account.children:
            subs = ET.SubElement(el, "SUBACCOUNTS")
            for child in account.children:
                ET.SubElement(subs, "SUBACCOUNT", {"id": child})
        if account.pairs:
            _pairs(el, account.pairs)

    def to_element(self):
        root = ET.Element("KMYMONEY-FILE")

        info = ET.SubElement(root, "FILEINFO")
        ET.SubElement(info, "CREATION_DATE", {"date": self.entrydate})
        ET.SubElement(info, "LAST_MODIFIED_DATE", {"date": self.entrydate})
        ET.SubElement(info, "VERSION", {"id": "1"})
        ET.SubElement(info, "FIXVERSION", {"id": "5"})

        user = ET.SubElement(root, "USER", {"name": "noname", "email": ""})
        ET.SubElement(user, "ADDRESS", {"zipcode": "", "city": "",
                                        "telephone": "", "street": "",
                                        "county": ""})

        institutions = ET.SubElement(
            root, "INSTITUTIONS", {"count": str(len(self.institution_order))})
        for ident in self.institution_order:
            inst = self.institutions[ident]
            el = ET.SubElement(institutions, "INSTITUTION", {
                "id": inst.id, "name": inst.name, "manager": "",
                "sortcode": "",
            })
            ET.SubElement(el, "ADDRESS", {"street": "", "telephone": "",
                                          "zip": "", "city": ""})
            ids = ET.SubElement(el, "ACCOUNTIDS")
            for account_id in inst.accounts:
                ET.SubElement(ids, "ACCOUNTID", {"id": account_id})

        payees = ET.SubElement(root, "PAYEES",
                               {"count": str(len(self.payee_order))})
        for name in self.payee_order:
            # The importer's own defaults: match this payee on an exact name.
            el = ET.SubElement(payees, "PAYEE", {
                "matchingenabled": "1",
                "matchignorecase": "1",
                "reference": "",
                "id": self.payees[name],
                "name": name,
                "usingmatchkey": "1",
                "matchkey": "^%s$" % name,
                "email": "",
            })
            ET.SubElement(el, "ADDRESS", {"postcode": "", "state": "",
                                          "city": "", "telephone": "",
                                          "street": ""})

        ET.SubElement(root, "COSTCENTERS", {"count": "0"})
        ET.SubElement(root, "TAGS", {"count": "0"})

        accounts = ET.SubElement(root, "ACCOUNTS",
                                 {"count": str(len(self.accounts))})
        for ident, _, _ in STANDARD_ACCOUNTS:
            self._account_element(accounts, self.accounts[ident])
        for ident in self.order:
            self._account_element(accounts, self.accounts[ident])

        transactions = ET.SubElement(root, "TRANSACTIONS",
                                     {"count": str(len(self.transactions))})
        for transaction in sorted(self.transactions,
                                  key=lambda t: (t.postdate, t.id)):
            el = ET.SubElement(transactions, "TRANSACTION", {
                "entrydate": self.entrydate,
                "postdate": transaction.postdate,
                "id": transaction.id,
                "commodity": self.currency,
                "memo": "",
            })
            splits = ET.SubElement(el, "SPLITS")
            for n, split in enumerate(transaction.splits, 1):
                ET.SubElement(splits, "SPLIT", {
                    "payee": split.payee,
                    "reconcileflag": str(split.reconcileflag),
                    "account": split.account,
                    "bankid": split.bankid,
                    "number": split.number,
                    "action": "",
                    "value": rational(split.value),
                    "shares": rational(split.value),
                    "id": "S%04d" % n,
                    "reconciledate": "",
                    "price": "1/1",
                    "memo": split.memo,
                })
            _pairs(el, {"Imported": "true"})

        _pairs(root, {"kmm-baseCurrency": self.currency,
                      "kmm-id": self.file_id})

        ET.SubElement(root, "SCHEDULES", {"count": "0"})
        ET.SubElement(root, "SECURITIES", {"count": "0"})
        currencies = ET.SubElement(root, "CURRENCIES", {"count": "1"})
        ET.SubElement(currencies, "CURRENCY", {
            "scf": "100", "type": "3", "pp": "4", "rounding-method": "7",
            "symbol": "$", "saf": "100", "id": self.currency,
            "name": "US Dollar",
        })
        ET.SubElement(root, "PRICES", {"count": "0"})
        ET.SubElement(root, "REPORTS", {"count": "0"})
        ET.SubElement(root, "BUDGETS", {"count": "0"})
        ET.SubElement(root, "ONLINEJOBS", {"count": "0"})
        return root

    def to_xml(self):
        root = self.to_element()
        ET.indent(root, space=" ")
        body = ET.tostring(root, encoding="unicode")
        # ElementTree writes "<TAG />"; KMyMoney writes "<TAG/>".  Both parse
        # the same, but matching it keeps our output diffable against a file
        # KMyMoney saved.  A bare '"' never survives into an attribute value --
        # ElementTree escapes it -- so this cannot match inside one.
        return XML_HEADER + body.replace('" />', '"/>') + "\n"

    def write(self, path, compress=True):
        data = self.to_xml().encode("utf-8")
        if not compress:
            with open(path, "wb") as fh:
                fh.write(data)
            return
        # filename="" keeps the member name out of the gzip header, which is
        # what KMyMoney itself writes.
        with open(path, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw) as fh:
                fh.write(data)


def _pairs(parent_el, mapping):
    el = ET.SubElement(parent_el, "KEYVALUEPAIRS")
    for key, value in mapping.items():
        ET.SubElement(el, "PAIR", {"value": value, "key": key})
    return el
