#!/usr/bin/env python3
"""
qif2kmy - build a KMyMoney (.kmy) file from a set of QIF files, one per
account, as written by mny2qif.

Usage:
    python qif2kmy.py QIF_OR_DIR... [-o FILE.kmy] [--list]
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import uuid
from fractions import Fraction

import kmymoney as kmm
import qif

# QIF file kind -> (KMyMoney account type, standard parent).
ACCOUNT_KIND = {
    "bank": (kmm.CHECKING, kmm.STD_ASSET),
    "ccard": (kmm.CREDIT_CARD, kmm.STD_LIABILITY),
    "cash": (kmm.CASH, kmm.STD_ASSET),
    "asset": (kmm.ASSET, kmm.STD_ASSET),
    "liability": (kmm.LIABILITY, kmm.STD_LIABILITY),
}

OPENING_BALANCES = "Opening Balances"

# Sidecar written by mny2qif, holding the account facts QIF has no field for.
MANIFEST_SUFFIX = "_accounts.json"


class Claim:
    """One side of a transfer, as named by a single QIF record or split line."""

    def __init__(self, owner, other, date, amount, entry, split=None):
        self.owner = owner      # account id the record belongs to
        self.other = other      # account id it transfers to
        self.date = date
        self.amount = amount
        self.entry = entry
        self.split = split      # None when the whole record is the transfer
        self.consumed = False

    @property
    def key(self):
        return (self.owner, self.other, self.date, self.amount)

    @property
    def counterkey(self):
        return (self.other, self.owner, self.date, -self.amount)


def bankid(account_name, entry, index):
    """A stable per-record import id, shaped like the ones KMyMoney writes.

    KMyMoney uses these only to recognise an already-imported record on a
    later import; the hash is ours, not a reproduction of KMyMoney's.
    """
    payload = "|".join([account_name, entry.date, str(entry.amount),
                        entry.payee, entry.memo, entry.number, str(index)])
    digest = hashlib.md5(payload.encode("utf-8")).hexdigest()[:7]
    return "%s-%s-1" % (entry.date, digest)


class Converter:
    def __init__(self, currency="USD", entrydate=None, file_id=None,
                 income=(), expense=(), closed=(), institutions=None):
        self.book = kmm.Book(
            currency=currency,
            entrydate=entrydate or datetime.date.today().isoformat(),
            file_id=file_id or "{%s}" % uuid.uuid4())
        self.warnings = []
        self.forced_income = {c.lower() for c in income}
        self.forced_expense = {c.lower() for c in expense}
        # Folded for matching, but keyed back to the spelling we were given so
        # that a warning quotes what the user or the manifest actually said.
        self.closed_names = {c.lower(): c for c in closed}
        self.institution_names = {k.lower(): v
                                  for k, v in (institutions or {}).items()}
        self.accounts = {}          # QIF account name -> account id
        self.sources = []           # (QifAccount, account id)
        self.categories = {}        # category path -> account id
        self.bankids = {}           # id(entry) -> import id for its own split
        self._imbalance = None
        self._equity = None

    def warn(self, message):
        self.warnings.append(message)

    # -- accounts --------------------------------------------------------

    def _register_accounts(self, parsed):
        """Create one account per QIF file, before any transfer is resolved.

        Doing every file-declared account first is what keeps a transfer such
        as L[Citi Costco] from a checking register pointing at the real credit
        card account instead of a fresh one invented on the spot.
        """
        for account in parsed:
            type_, parent = ACCOUNT_KIND[account.kind]
            existing = self.accounts.get(account.name)
            if existing is not None:
                self.warn("%s: account %r is already declared by another file; "
                          "merging the two registers"
                          % (os.path.basename(account.path), account.name))
                self.sources.append((account, existing))
                continue

            ident = self.book.add_account(
                account.name, type_, parent,
                opened=account.opening_date or "",
                description="Imported from %s"
                            % os.path.basename(account.path))
            dates = [e.date for e in account.entries if e.date]
            if dates:
                self.book.accounts[ident].pairs["lastImportedTransactionDate"] \
                    = max(dates)
            self.accounts[account.name] = ident
            self.sources.append((account, ident))

        for account, _ in self.sources:
            for index, entry in enumerate(account.entries, 1):
                if entry.date:
                    self.bankids[id(entry)] = bankid(account.name, entry, index)

    def transfer_target(self, name, entry):
        """Resolve L[name], inventing an asset account if no file declared it."""
        if name not in self.accounts:
            self.accounts[name] = self.book.add_account(
                name, kmm.ASSET, kmm.STD_ASSET,
                opened=entry.date or "",
                description="Transfer target with no QIF file of its own")
            self.warn("%s: transfer to %r, which no input file declares; "
                      "created it as an asset account"
                      % (entry.where, name))
        return self.accounts[name]

    @property
    def imbalance(self):
        if self._imbalance is None:
            self._imbalance = self.book.add_account(
                "Imbalance-%s" % self.book.currency, kmm.EXPENSE,
                kmm.STD_EXPENSE, opened=kmm.CATEGORY_OPENED)
        return self._imbalance

    @property
    def equity(self):
        if self._equity is None:
            self._equity = self.book.add_account(
                OPENING_BALANCES, kmm.EQUITY, kmm.STD_EQUITY,
                opened=kmm.CATEGORY_OPENED)
        return self._equity

    # -- categories ------------------------------------------------------

    def _classify_categories(self, parsed):
        """Decide income or expense for every category named in the input.

        QIF carries no such marking, so the sign decides it, as KMyMoney's own
        importer does: money arriving in the account came from income, money
        leaving it went to an expense.  A category used both ways goes with the
        majority of its uses, and --income/--expense override the result.
        """
        votes = {}
        for account, _ in self.sources:
            for entry in account.entries:
                uses = [(entry.category, entry.amount)]
                if entry.splits:
                    uses = [(s.category, s.amount) for s in entry.splits]
                for path, amount in uses:
                    if not path:
                        continue
                    tally = votes.setdefault(path, [0, 0])
                    tally[0 if amount > 0 else 1] += 1

        for path, (inflow, outflow) in votes.items():
            if path.lower() in self.forced_income:
                type_ = kmm.INCOME
            elif path.lower() in self.forced_expense:
                type_ = kmm.EXPENSE
            elif inflow and outflow:
                type_ = kmm.INCOME if inflow > outflow else kmm.EXPENSE
                self.warn("category %r is used %d time(s) as income and %d as "
                          "expense; treating it as %s (override with --income "
                          "or --expense)"
                          % (path, inflow, outflow,
                             "income" if type_ == kmm.INCOME else "expense"))
            else:
                type_ = kmm.INCOME if inflow else kmm.EXPENSE
            self.categories[path] = self.book.category(path, type_)

    # -- transfers -------------------------------------------------------

    def _collect_claims(self):
        """Index every transfer leg in the input so the two sides can pair up."""
        index = {}
        for account, ident in self.sources:
            for entry in account.entries:
                if not entry.date:
                    continue
                legs = []
                if entry.splits:
                    legs = [(s.transfer, s.amount, s) for s in entry.splits
                            if s.transfer]
                elif entry.transfer:
                    legs = [(entry.transfer, entry.amount, None)]
                for name, amount, split in legs:
                    other = self.transfer_target(name, entry)
                    claim = Claim(ident, other, entry.date, amount, entry,
                                  split)
                    index.setdefault(claim.key, []).append(claim)
        return index

    def _pair_transfers(self, index):
        """Match the two halves of each transfer.

        Money writes a transfer into both registers, so every pair would
        otherwise import twice.  Two simple records merge into one transaction;
        a simple record facing a split line is dropped, because the split
        transaction already carries both sides.
        """
        pairs = {}              # id(entry) -> the entry it merges with
        dropped = set()         # id(entry) for records a split already covers
        for account, ident in self.sources:
            for entry in account.entries:
                if entry.splits or not entry.transfer or not entry.date:
                    continue
                claim = next((c for c in index.get(
                    (ident, self.accounts[entry.transfer], entry.date,
                     entry.amount), []) if c.entry is entry), None)
                if claim is None or claim.consumed:
                    continue
                counter = next((c for c in index.get(claim.counterkey, [])
                                if not c.consumed and c.entry is not entry),
                               None)
                if counter is None:
                    continue
                claim.consumed = counter.consumed = True
                if counter.split is None:
                    pairs[id(entry)] = counter.entry
                    pairs[id(counter.entry)] = entry
                else:
                    dropped.add(id(entry))
        return pairs, dropped

    # -- transactions ----------------------------------------------------

    def _opening_balances(self):
        for account, ident in self.sources:
            if not account.opening_balance:
                continue
            if not account.opening_date:
                self.warn("%s: opening balance of %s has no date; skipped"
                          % (os.path.basename(account.path),
                             account.opening_balance))
                continue
            self.book.add_transaction(account.opening_date, [
                kmm.Split(ident, account.opening_balance,
                          reconcileflag=account.opening_reconcileflag,
                          memo=OPENING_BALANCES),
                kmm.Split(self.equity, -account.opening_balance,
                          reconcileflag=account.opening_reconcileflag,
                          memo=OPENING_BALANCES),
            ])

    def _other_side(self, entry, amount, memo, payee):
        """The split facing the account's own side of a simple record."""
        if entry.transfer:
            account = self.accounts[entry.transfer]
        elif entry.category:
            account = self.categories[entry.category]
        else:
            account = self.imbalance
            self.warn("%s: record has no category and no transfer; assigned to "
                      "%s" % (entry.where, self.book.accounts[account].name))
        return kmm.Split(account, -amount, payee=payee, memo=memo)

    def _build(self, pairs, dropped):
        seen = set()
        for account, ident in self.sources:
            for entry in account.entries:
                if id(entry) in dropped or id(entry) in seen:
                    continue
                if not entry.date:
                    self.warn("%s: record has no date; skipped" % entry.where)
                    continue
                seen.add(id(entry))
                payee = self.book.payee(entry.payee)
                own = kmm.Split(ident, entry.amount, payee=payee,
                                reconcileflag=entry.reconcileflag,
                                memo=entry.memo, number=entry.number,
                                bankid=self.bankids[id(entry)])

                partner = pairs.get(id(entry))
                if partner is not None:
                    seen.add(id(partner))
                    other = self.accounts[entry.transfer]
                    if not own.payee:
                        own.payee = self.book.payee(partner.payee)
                    splits = [own, kmm.Split(
                        other, partner.amount, payee=own.payee,
                        reconcileflag=partner.reconcileflag,
                        memo=partner.memo or entry.memo,
                        bankid=self.bankids[id(partner)])]
                elif entry.splits:
                    splits = [own] + self._split_detail(entry, payee)
                else:
                    splits = [own, self._other_side(entry, entry.amount,
                                                    entry.memo, payee)]

                remainder = -sum((s.value for s in splits), Fraction(0))
                if remainder:
                    self.warn("%s: splits are off by %s; the difference went "
                              "to %s" % (entry.where, remainder,
                                         self.book.accounts[
                                             self.imbalance].name))
                    splits.append(kmm.Split(self.imbalance, remainder,
                                            payee=payee))
                self.book.add_transaction(entry.date, splits)

    def _split_detail(self, entry, payee):
        splits = []
        for line in entry.splits:
            if line.transfer:
                account = self.accounts[line.transfer]
            elif line.category:
                account = self.categories[line.category]
            else:
                account = self.imbalance
                self.warn("%s: split line has no category; assigned to %s"
                          % (entry.where, self.book.accounts[account].name))
            splits.append(kmm.Split(account, -line.amount, payee=payee,
                                    memo=line.memo))
        return splits

    # -- closed accounts -------------------------------------------------

    def _mark_closed(self):
        """Flag the accounts Money had closed.

        Run last, so that it covers both accounts that had a QIF file of their
        own and ones invented for a transfer -- an account left out of the
        export by --skip-closed is still named by the transfers that reached
        it, and must not come back reopened.
        """
        for name, ident in self.accounts.items():
            if name.lower() in self.closed_names:
                self.book.accounts[ident].pairs[kmm.CLOSED_KEY] = \
                    kmm.CLOSED_VALUE

        known = {name.lower() for name in self.accounts}
        for folded in sorted(set(self.closed_names) - known):
            self.warn("%r is listed as closed but no account of that name was "
                      "built; nothing to close" % self.closed_names[folded])

    def _assign_institutions(self):
        for name, ident in self.accounts.items():
            institution = self.institution_names.get(name.lower())
            if institution:
                self.book.set_institution(ident, institution)

    # -- driver ----------------------------------------------------------

    def convert(self, paths):
        parsed = [qif.load(p) for p in paths]
        self._register_accounts(parsed)
        self._classify_categories(parsed)
        self._opening_balances()
        index = self._collect_claims()
        self._build(*self._pair_transfers(index))
        self._mark_closed()
        self._assign_institutions()
        return self.book


def expand(paths):
    """Expand directory arguments into the QIF files they hold."""
    out = []
    for path in paths:
        if os.path.isdir(path):
            # Matched by suffix rather than by glob, so that a case-insensitive
            # file system does not hand back every file twice.
            found = sorted(os.path.join(path, n) for n in os.listdir(path)
                           if n.lower().endswith(".qif"))
            if not found:
                raise qif.QifError("no .qif files in %s" % path)
            out += found
        else:
            out.append(path)
    return out


def find_manifests(paths):
    """Look for mny2qif's sidecar in each directory the QIF files came from."""
    found = []
    for directory in dict.fromkeys(os.path.dirname(p) or "." for p in paths):
        found += sorted(os.path.join(directory, name)
                        for name in os.listdir(directory)
                        if name.endswith(MANIFEST_SUFFIX))
    return found


def closed_from_manifests(paths):
    """Read the account names mny2qif recorded as closed."""
    names = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        for account in manifest.get("accounts", []):
            if account.get("closed"):
                names.append(account["name"])
    return names


def institutions_from_manifests(paths):
    """Read the account name -> institution name pairs mny2qif recorded."""
    found = {}
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        for account in manifest.get("accounts", []):
            if account.get("institution"):
                found[account["name"]] = account["institution"]
    return found

def default_output(paths):
    """Name the book after the common prefix of its inputs, as mny2qif names
    its files after the .mny file.

    commonprefix() works a character at a time, so ms_money_cd and
    ms_money_checking share "ms_money_c".  Unless the prefix is a whole input
    name, it is cut back to the last word boundary to get "ms_money".
    """
    stems = [os.path.splitext(os.path.basename(p))[0] for p in paths]
    stem = os.path.commonprefix(stems)
    if stem not in stems:
        stem = stem[:max(stem.rfind("_"), stem.rfind("-")) + 1]
    return (stem.rstrip("_- ") or "accounts") + ".kmy"


def build_parser():
    p = argparse.ArgumentParser(
        prog="qif2kmy",
        description="Build a KMyMoney file from QIF files, one per account.")
    p.add_argument("qif", nargs="+", metavar="QIF_OR_DIR",
                   help="QIF files to read, or directories of them")
    p.add_argument("-o", "--output", metavar="FILE.kmy",
                   help="file to write (default: the inputs' common prefix "
                        "plus .kmy, in the current directory)")
    p.add_argument("-l", "--list", action="store_true",
                   help="print the accounts, categories and transaction count "
                        "and exit without writing anything")
    p.add_argument("--plain", action="store_true",
                   help="write uncompressed XML; KMyMoney reads both, but it "
                        "writes gzip, which is the default here")
    p.add_argument("--currency", default="USD",
                   help="ISO currency code for every account (default: USD)")
    p.add_argument("--income", action="append", default=[], metavar="CATEGORY",
                   help="force this category to be income, overriding the "
                        "sign heuristic; repeatable")
    p.add_argument("--expense", action="append", default=[], metavar="CATEGORY",
                   help="force this category to be an expense; repeatable")
    p.add_argument("--closed", action="append", default=[], metavar="ACCOUNT",
                   help="mark this account closed in KMyMoney, on top of "
                        "whatever the manifest says; repeatable")
    p.add_argument("--manifest", action="append", default=[], metavar="FILE",
                   help="mny2qif's %s sidecar, naming the accounts Money had "
                        "closed (default: any found beside the QIF files)"
                        % MANIFEST_SUFFIX)
    p.add_argument("--no-manifest", dest="use_manifest", action="store_false",
                   help="ignore the sidecar and take closed accounts only "
                        "from --closed")
    p.add_argument("--entry-date", metavar="YYYY-MM-DD",
                   help="value for the file's creation and entry dates "
                        "(default: today)")
    p.add_argument("--file-id", metavar="UUID",
                   help="value for kmm-id (default: a fresh UUID)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        paths = expand(args.qif)
        manifests = list(args.manifest)
        if args.use_manifest and not manifests:
            manifests = find_manifests(paths)
        for path in manifests:
            print("reading %s" % path)
        closed = args.closed + closed_from_manifests(manifests)

        converter = Converter(currency=args.currency,
                              entrydate=args.entry_date,
                              file_id=args.file_id,
                              income=args.income, expense=args.expense,
                              closed=closed,
                              institutions=institutions_from_manifests(
                                  manifests))
        book = converter.convert(paths)
    except (OSError, ValueError, qif.QifError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1

    for w in converter.warnings:
        sys.stderr.write("warning: %s\n" % w)

    def is_closed(ident):
        return kmm.CLOSED_KEY in book.accounts[ident].pairs

    shut = sum(1 for a in book.accounts.values()
               if kmm.CLOSED_KEY in a.pairs)
    summary = "%d accounts%s, %d payees, %d transactions" % (
        len(book.accounts), " (%d closed)" % shut if shut else "",
        len(book.payee_order), len(book.transactions))

    if args.list:
        for name, ident in converter.accounts.items():
            source = next((a for a, i in converter.sources if a.name == name),
                          None)
            print("%-8s %-40s %-12s %s%s"
                  % (ident, name, source.kind if source else "no file",
                     "%5d entries" % len(source.entries) if source
                     else "transfer target only",
                     "  closed" if is_closed(ident) else ""))
        for path in sorted(converter.categories):
            ident = converter.categories[path]
            print("%-8s %-40s %s" % (ident, path,
                                     "income"
                                     if book.accounts[ident].type == kmm.INCOME
                                     else "expense"))
        print(summary)
        return 0

    out = args.output or default_output(paths)
    book.write(out, compress=not args.plain)
    print("%s  (%s)" % (out, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
