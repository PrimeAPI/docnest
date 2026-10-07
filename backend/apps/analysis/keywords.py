"""Built-in keyword knowledge for a useful first run (before any training data exists).

Keywords are matched against folded words of the document (umlauts folded,
lowercase). Tags are created as *suggested* the first time they are used, with
German aliases so that a user's own German tags are reused instead of
duplicated.
"""

from __future__ import annotations

# type slug -> keywords (prefixes of folded words)
TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "invoice": ("rechnung", "invoice", "rechnungsnummer", "rechnungsbetrag", "beitragsrechnung"),
    "contract": ("vertrag", "vertragsbedingungen", "contract", "vereinbarung", "agreement", "versicherungsschein"),
    "notice": (
        "bescheid", "festsetzung", "steuerbescheid", "notice of assessment",
        # Official mail from authorities: certificates and register extracts
        "bundesamt", "bundeszentralamt", "landratsamt", "buergeramt", "standesamt", "fuehrungszeugnis",
        "bescheinigung", "meldebescheinigung", "behoerde",
    ),
    "statement": (
        "abrechnung", "kontoauszug", "gehaltsabrechnung", "entgeltabrechnung", "lohnabrechnung", "statement",
        "depotauszug", "jahresabrechnung", "verdienstabrechnung",
    ),
}  # fmt: skip

# tag name -> (aliases, keywords)
TAG_KEYWORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Vehicle": (
        ("Fahrzeug", "Auto", "KFZ", "Kfz", "PKW", "Automobil", "Kraftfahrzeug", "Car"),
        ("fahrzeug", "kfz", "pkw", "kraftfahrzeug", "kennzeichen", "fahrgestellnummer", "werkstatt", "tuev",
         "hauptuntersuchung", "motorrad", "vehicle"),
    ),
    "Insurance": (
        ("Versicherung", "Versicherungen"),
        ("versicherung", "versicherungsschein", "versicherungsnehmer", "police", "insurance", "beitrag"),
    ),
    "Taxes": (("Steuer", "Steuern", "Finanzamt"), ("finanzamt", "steuer", "einkommensteuer", "steuernummer", "tax")),
    "Salary": (
        ("Gehalt", "Lohn", "Gehaltsabrechnung"),
        ("gehaltsabrechnung", "entgeltabrechnung", "lohnabrechnung", "verdienstabrechnung", "bruttolohn",
         "nettoverdienst", "payslip", "salary"),
    ),
    "Banking": (("Bank", "Konto"), ("kontoauszug", "girokonto", "depot", "sparkasse", "volksbank", "kreditkarte")),
    "Energy": (("Strom", "Gas", "Energie"), ("strom", "stromlieferung", "erdgas", "kwh", "zaehlerstand", "energie")),
    "Telecom": (
        ("Mobilfunk", "Telefon", "Internet", "Handy"),
        ("mobilfunk", "telekom", "vodafone", "telefonica", "festnetz", "dsl", "datenvolumen"),
    ),
    "Health": (
        ("Gesundheit", "Arzt", "Krankenkasse"),
        ("krankenkasse", "krankenversicherung", "arzt", "praxis", "behandlung", "rezept", "patient", "aok", "barmer"),
    ),
    "Housing": (("Wohnung", "Miete", "Nebenkosten"), ("miete", "mietvertrag", "vermieter", "nebenkosten", "wohnung")),
    "Studies": (
        ("Studium", "Uni", "Universität", "Hochschule"),
        ("universitaet", "hochschule", "semester", "immatrikulation", "matrikelnummer", "studierende", "pruefung"),
    ),
    "Pension": (("Rente", "Altersvorsorge"), ("rentenversicherung", "renteninformation", "altersvorsorge", "pension")),
}  # fmt: skip

MIN_KEYWORD_HITS = 2
