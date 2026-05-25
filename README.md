# genopro-to-gedcom

A small, zero-dependency Python script that converts GenoPro family tree
files (`.gno`) into standard GEDCOM 5.5.1 files (`.ged`) that can be
opened in any modern genealogy program: Gramps, RootsMagic,
MacFamilyTree, Family Tree Maker, MyHeritage Family Tree Builder, and
similar.

GenoPro stopped being the dominant genogram tool a long time ago, and
many people have old `.gno` files sitting around with no easy way to
view them anymore. This converter unlocks that data without requiring
you to buy or install GenoPro.

## Why this exists

A `.gno` file is a ZIP archive containing a single `Data.xml` file with
all the genealogy data inside. The XML format is well-structured but
proprietary, and no major genealogy program reads it directly. GEDCOM
5.5.1 is the universal interchange format that every genealogy tool
supports, so converting once frees the data forever.

## Requirements

- Python 3.8 or newer
- No external dependencies (uses only the standard library)

## Usage

Extract the XML from your `.gno` file first (it's just a ZIP):

```bash
unzip YourFamilyTree.gno
```

This produces `Data.xml`. Then run the converter:

```bash
python3 convert.py Data.xml YourFamilyTree.ged
```

Drop the resulting `.ged` file into your genealogy software via its
"Import GEDCOM" option.

## What the converter handles

| GenoPro element | GEDCOM tag | Notes |
|---|---|---|
| `<Individual>` | `INDI` | |
| `<Name><Display>` | `1 NAME` | Preferred over `<First>` since Display is what was rendered on the original chart |
| `<First>` | `2 GIVN` | |
| `<Last>` | `2 SURN` | Only emitted when present |
| `<Gender>` | `1 SEX` | M/F preserved; missing becomes U |
| `<Birth><Date>` | `1 BIRT` / `2 DATE` | Month uppercased per GEDCOM spec |
| `<Birth><Place>` | `1 BIRT` / `2 PLAC` | Resolved through the `Places` table |
| `<Occupations>` ref | `1 OCCU` | Resolved through `Occupations` table |
| `<Educations>` ref | `1 EDUC` | Resolved through `Educations` table |
| `<Contacts>` Telephone | `1 PHON` | |
| `<Contacts>` Email | `1 EMAIL` | |
| `<custom_tag1>` | `1 NOTE` | Multi-line text split with CONT |
| `<Family>` | `FAM` | |
| `PedigreeLink Parent` | `HUSB` or `WIFE` | Assigned by the individual's sex |
| `PedigreeLink Biological` | `CHIL` | |
| `PedigreeLink Adopted/Foster/Step` | `CHIL` with `PEDI` | |
| `<Label>` records | `NOTE` in HEAD | Canvas annotations preserved |

## Things to know

**Patrilineal trees work fine.** Many older `.gno` files only record
fathers, not mothers. The converter handles families with one parent,
two parents, or (rare) same-sex parents gracefully.

**Names with no surname.** GenoPro often stores the full name in
`<First>` with no `<Last>` value. The converter preserves the full
given name and only emits a `SURN` line when an explicit surname exists.

**Layout data is dropped.** GenoPro stores x/y coordinates for every
individual to render the chart. GEDCOM has no equivalent, so the visual
layout you originally drew is not preserved. Your importing program
will re-layout the tree using its own algorithm.

**Pictures are referenced but not embedded.** If your `.gno` referenced
external image files (typically a sibling `Pictures/` folder), those
references are not currently emitted as `OBJE` records. Easy to add if
you need it.

## Output validation

The output passes round-trip parsing with `ged4py` and structural lint
against the GEDCOM 5.5.1 grammar:

- Every cross-reference resolves
- No line exceeds the 255-character limit
- Level sequences never skip
- Header includes the required SOUR, GEDC, and CHAR tags
- Every level-0 record except HEAD/TRLR has a valid xref id

## License

MIT. See `LICENSE`.

## Contributing

Pull requests welcome. The XML format has some optional fields the
converter does not yet handle (e.g. `<Death>`, `<Marriage>`, `OBJE` /
picture references). If your `.gno` has data the converter misses, open
an issue with a redacted XML sample.
