#!/usr/bin/env python3
"""
GenoPro (.gno) XML to GEDCOM 5.5.1 converter.

GenoPro stores its data as a single Data.xml file inside a ZIP archive
with the .gno extension. This script converts that XML into a standard
GEDCOM 5.5.1 file that any modern genealogy program (Gramps, RootsMagic,
MacFamilyTree, Family Tree Maker, etc.) can import.

Usage:
    python3 convert.py input.xml output.ged

The script is deliberately tolerant of missing fields. GenoPro lets you
record a family tree with almost no metadata (just names and relationships)
and that is fine; the converter only emits GEDCOM tags for data that
actually exists in the source.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# GEDCOM writing helpers
# ---------------------------------------------------------------------------

# Max value length per GEDCOM line (the spec allows up to 255 chars total
# including level and tag; we play it safe at 200 for the value portion).
MAX_VALUE_LEN = 200


def gedcom_line(level: int, tag: str, value: str = "", xref: str = "") -> str:
    """Build a single GEDCOM line. xref is the @ID@ for level-0 records."""
    parts = [str(level)]
    if xref:
        parts.append(xref)
    parts.append(tag)
    if value:
        parts.append(value)
    return " ".join(parts)


def emit_text(out: list[str], level: int, tag: str, text: str) -> None:
    """
    Emit a multi-line text value using CONT (line break) and CONC
    (continuation without break) as GEDCOM 5.5.1 requires.
    """
    if text is None:
        return
    # Split on explicit newlines first; each becomes its own CONT.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first = True
    for line in lines:
        # If a single logical line is longer than MAX_VALUE_LEN, break it
        # further with CONC (no implied newline).
        if not line:
            # Preserve empty lines as CONT with empty value.
            if first:
                out.append(gedcom_line(level, tag))
                first = False
            else:
                out.append(gedcom_line(level + 1, "CONT"))
            continue
        chunks = [line[i : i + MAX_VALUE_LEN] for i in range(0, len(line), MAX_VALUE_LEN)]
        for i, chunk in enumerate(chunks):
            if first:
                out.append(gedcom_line(level, tag, chunk))
                first = False
            elif i == 0:
                out.append(gedcom_line(level + 1, "CONT", chunk))
            else:
                out.append(gedcom_line(level + 1, "CONC", chunk))


def normalize_xref(raw_id: str) -> str:
    """Turn a GenoPro internal id (e.g. ind00024, fam00012, I1) into a
    GEDCOM xref (@IND00024@). GEDCOM xrefs must start with a letter and
    contain only letters/digits/underscores."""
    return "@" + raw_id.upper() + "@"


# ---------------------------------------------------------------------------
# GenoPro reader
# ---------------------------------------------------------------------------


class GenoProTree:
    """Parsed GenoPro XML, ready to walk for GEDCOM emission."""

    def __init__(self, xml_path: str):
        self.tree = ET.parse(xml_path)
        self.root = self.tree.getroot()

        # Header metadata
        self.source_version = self._text("./Software/Version", default="unknown")
        self.source_date = self._text("./Date", default="")

        # Lookup tables for referenced records
        self.places = self._index_by_id("Places", "Place")
        self.educations = self._index_by_id("Educations", "Education")
        self.occupations = self._index_by_id("Occupations", "Occupation")
        self.contacts = self._index_by_id("Contacts", "Contact")
        self.pictures = self._index_by_id("Pictures", "Picture")

        # Labels (free-text notes drawn on the canvas) - we put these in
        # the header so the user does not lose attribution / sources.
        self.labels: list[str] = []
        labels_el = self.root.find("Labels")
        if labels_el is not None:
            for label in labels_el.findall("Label"):
                text = label.findtext("Text", "").strip()
                if text:
                    self.labels.append(text)

        # Build family -> [parents], family -> [children] from PedigreeLinks
        self.fam_parents: dict[str, list[str]] = defaultdict(list)
        self.fam_children: dict[str, list[str]] = defaultdict(list)
        # Also reverse: individual -> [families as spouse], [families as child]
        self.ind_fams: dict[str, list[str]] = defaultdict(list)
        self.ind_famc: dict[str, list[str]] = defaultdict(list)

        # Fast id -> Individual element lookup
        self.ind_by_id: dict[str, ET.Element] = {}
        ind_container = self.root.find("Individuals")
        if ind_container is not None:
            for ind in ind_container.findall("Individual"):
                iid = ind.get("ID")
                if iid:
                    self.ind_by_id[iid] = ind

        pl = self.root.find("PedigreeLinks")
        if pl is not None:
            for link in pl.findall("PedigreeLink"):
                rel = link.get("PedigreeLink")
                fam = link.get("Family")
                ind = link.get("Individual")
                if not fam or not ind:
                    continue
                if rel == "Parent":
                    self.fam_parents[fam].append(ind)
                    self.ind_fams[ind].append(fam)
                elif rel in ("Biological", "Adopted", "Foster", "Step"):
                    # GenoPro uses these PedigreeLink types for child links.
                    self.fam_children[fam].append((ind, rel))
                    self.ind_famc[ind].append((fam, rel))

    def _text(self, xpath: str, default: str = "") -> str:
        el = self.root.find(xpath)
        if el is None or el.text is None:
            return default
        return el.text.strip()

    def _index_by_id(self, container_tag: str, item_tag: str) -> dict[str, ET.Element]:
        result: dict[str, ET.Element] = {}
        container = self.root.find(container_tag)
        if container is None:
            return result
        for item in container.findall(item_tag):
            iid = item.get("ID")
            if iid:
                result[iid] = item
        return result

    # ------------------------------------------------------------------
    # Individual extraction
    # ------------------------------------------------------------------

    def individuals(self):
        container = self.root.find("Individuals")
        if container is None:
            return
        for ind in container.findall("Individual"):
            yield ind

    def families(self):
        container = self.root.find("Families")
        if container is None:
            return
        for fam in container.findall("Family"):
            yield fam

    @staticmethod
    def best_name(ind: ET.Element) -> tuple[str, Optional[str]]:
        """
        Return (given_name, surname_or_None) for an individual.

        GenoPro stores names in up to four places:
          - raw text inside <Name>      (may diverge from the chart label)
          - <Display>                   (what was actually shown on the chart)
          - <First>                     (a "first/given name" field)
          - <Last>                      (a surname / lineage tag, often empty)

        We trust Display first, fall back to First, then to the raw Name
        text. Surname is taken from Last if present.
        """
        name_el = ind.find("Name")
        if name_el is None:
            return ("Unknown", None)

        display = (name_el.findtext("Display") or "").strip()
        first = (name_el.findtext("First") or "").strip()
        last = (name_el.findtext("Last") or "").strip()
        raw = (name_el.text or "").strip()

        given = display or first or raw or "Unknown"
        surname = last or None
        return given, surname


# ---------------------------------------------------------------------------
# GEDCOM emitter
# ---------------------------------------------------------------------------


def emit_header(tree: GenoProTree, out: list[str]) -> None:
    today = datetime.now().strftime("%d %b %Y").upper()
    out.append(gedcom_line(0, "HEAD"))
    out.append(gedcom_line(1, "SOUR", "GENOPRO_TO_GEDCOM"))
    out.append(gedcom_line(2, "NAME", "GenoPro XML to GEDCOM Converter"))
    out.append(gedcom_line(2, "VERS", "1.0"))
    out.append(gedcom_line(2, "CORP", "Custom converter"))
    out.append(gedcom_line(1, "DEST", "ANY"))
    out.append(gedcom_line(1, "DATE", today))
    out.append(gedcom_line(1, "CHAR", "UTF-8"))
    out.append(gedcom_line(1, "GEDC"))
    out.append(gedcom_line(2, "VERS", "5.5.1"))
    out.append(gedcom_line(2, "FORM", "LINEAGE-LINKED"))
    out.append(gedcom_line(1, "LANG", "English"))

    # Build a NOTE block preserving provenance.
    note_lines = [
        f"Converted from GenoPro {tree.source_version} file dated {tree.source_date}.",
    ]
    if tree.labels:
        note_lines.append("")
        note_lines.append("Original canvas labels:")
        for label in tree.labels:
            note_lines.append("")
            note_lines.append(label)
    emit_text(out, 1, "NOTE", "\n".join(note_lines))


def emit_individual(tree: GenoProTree, ind: ET.Element, out: list[str]) -> None:
    ind_id = ind.get("ID")
    if not ind_id:
        return
    xref = normalize_xref(ind_id)
    out.append(gedcom_line(0, "INDI", xref=xref))

    given, surname = GenoProTree.best_name(ind)
    if surname:
        name_value = f"{given} /{surname}/"
    else:
        name_value = given
    out.append(gedcom_line(1, "NAME", name_value))
    out.append(gedcom_line(2, "GIVN", given))
    if surname:
        out.append(gedcom_line(2, "SURN", surname))

    # Sex
    gender = (ind.findtext("Gender") or "").strip().upper()
    if gender in ("M", "F"):
        out.append(gedcom_line(1, "SEX", gender))
    else:
        out.append(gedcom_line(1, "SEX", "U"))

    # Birth event - emitted only if a date or place exists.
    birth = ind.find("Birth")
    if birth is not None:
        date_text = (birth.findtext("Date") or "").strip()
        place_ref = (birth.findtext("Place") or "").strip()
        place_name = None
        if place_ref and place_ref in tree.places:
            place_name = (tree.places[place_ref].findtext("Name") or "").strip() or None
        if date_text or place_name:
            out.append(gedcom_line(1, "BIRT"))
            if date_text:
                out.append(gedcom_line(2, "DATE", date_text.upper()))
            if place_name:
                out.append(gedcom_line(2, "PLAC", place_name))

    # Occupation
    occ_ref = (ind.findtext("Occupations") or "").strip()
    if occ_ref and occ_ref in tree.occupations:
        title = (tree.occupations[occ_ref].findtext("Title") or "").strip()
        if title:
            out.append(gedcom_line(1, "OCCU", title))

    # Education
    edu_ref = (ind.findtext("Educations") or "").strip()
    if edu_ref and edu_ref in tree.educations:
        institution = (tree.educations[edu_ref].findtext("Institution") or "").strip()
        if institution:
            out.append(gedcom_line(1, "EDUC", institution))

    # Contact info (phone, email)
    contact_ref = (ind.findtext("Contacts") or "").strip()
    if contact_ref and contact_ref in tree.contacts:
        contact = tree.contacts[contact_ref]
        phone = (contact.findtext("Telephone") or "").strip()
        email = (contact.findtext("Email") or "").strip()
        if phone:
            out.append(gedcom_line(1, "PHON", phone))
        if email:
            out.append(gedcom_line(1, "EMAIL", email))

    # Family links
    for fam_id in tree.ind_famc.get(ind_id, []):
        # ind_famc holds (family, relationship) tuples
        fam, rel = fam_id
        out.append(gedcom_line(1, "FAMC", normalize_xref(fam)))
        # Non-biological child relationships are noted with PEDI
        pedi_map = {"Adopted": "adopted", "Foster": "foster", "Step": "step"}
        if rel in pedi_map:
            out.append(gedcom_line(2, "PEDI", pedi_map[rel]))
    for fam in tree.ind_fams.get(ind_id, []):
        out.append(gedcom_line(1, "FAMS", normalize_xref(fam)))

    # Custom_tag1 -> NOTE
    custom = ind.find("custom_tag1")
    if custom is not None and custom.text:
        emit_text(out, 1, "NOTE", custom.text.strip())


def emit_family(tree: GenoProTree, fam: ET.Element, out: list[str]) -> None:
    fam_id = fam.get("ID")
    if not fam_id:
        return
    xref = normalize_xref(fam_id)
    out.append(gedcom_line(0, "FAM", xref=xref))

    parents = tree.fam_parents.get(fam_id, [])
    # Assign HUSB and WIFE based on the individual's recorded sex.
    husb: Optional[str] = None
    wife: Optional[str] = None
    leftovers: list[str] = []
    for p in parents:
        ind = tree.ind_by_id.get(p)
        gender = (ind.findtext("Gender") if ind is not None else "") or ""
        gender = gender.strip().upper()
        if gender == "M" and husb is None:
            husb = p
        elif gender == "F" and wife is None:
            wife = p
        else:
            leftovers.append(p)
    # If we still have unassigned parents (e.g. two males in one family),
    # fill the empty slot to avoid losing them; this is rare and noted.
    for p in leftovers:
        if husb is None:
            husb = p
        elif wife is None:
            wife = p

    if husb:
        out.append(gedcom_line(1, "HUSB", normalize_xref(husb)))
    if wife:
        out.append(gedcom_line(1, "WIFE", normalize_xref(wife)))
    for child_id, _rel in tree.fam_children.get(fam_id, []):
        out.append(gedcom_line(1, "CHIL", normalize_xref(child_id)))


def emit_trailer(out: list[str]) -> None:
    out.append(gedcom_line(0, "TRLR"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def convert(xml_path: str, ged_path: str) -> dict:
    tree = GenoProTree(xml_path)
    out: list[str] = []
    emit_header(tree, out)

    n_ind = 0
    for ind in tree.individuals():
        emit_individual(tree, ind, out)
        n_ind += 1

    n_fam = 0
    for fam in tree.families():
        emit_family(tree, fam, out)
        n_fam += 1

    emit_trailer(out)

    # GEDCOM files are conventionally CRLF-terminated, but most modern
    # parsers accept LF. We write CRLF for maximum compatibility.
    with open(ged_path, "w", encoding="utf-8", newline="") as f:
        for line in out:
            f.write(line + "\r\n")

    return {"individuals": n_ind, "families": n_fam, "lines": len(out)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("xml", help="Path to extracted GenoPro Data.xml")
    p.add_argument("ged", help="Path to write the GEDCOM .ged output")
    args = p.parse_args()

    stats = convert(args.xml, args.ged)
    print(
        f"Wrote {stats['lines']} GEDCOM lines covering "
        f"{stats['individuals']} individuals and {stats['families']} families."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
