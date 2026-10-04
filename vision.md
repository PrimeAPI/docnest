# Secure Document Management Service

## 1. Projektziel

Ziel des Projekts ist die Entwicklung eines selbst gehosteten, Open-Source-basierten Dokumentenmanagementsystems für die sichere Digitalisierung, Verarbeitung, Ablage und Suche von Dokumenten.

Das System soll insbesondere für automatisierte Scan-Workflows geeignet sein. Dokumente werden beispielsweise durch dedizierte Scanner-Hardware erfasst und anschließend über eine Upload-API an den Server übertragen.

Nach dem Upload übernimmt der Server die weitere Verarbeitung vollständig automatisiert. Dazu gehören unter anderem:

- sichere Übernahme des Dokuments
- OCR-Verarbeitung
- Extraktion relevanter Metadaten
- Klassifizierung des Dokuments
- automatische Vergabe von Tags
- Erkennung wiederkehrender Dokumentarten
- Ablage im entsprechenden Bucket
- Bereitstellung für Suche und Weboberfläche

Die Benutzeroberfläche soll bewusst einfach gehalten werden und sich auf schnelles Finden, Verwalten und Bearbeiten von Dokumenten konzentrieren.

Ein wesentlicher Schwerpunkt des gesamten Projekts ist die **Datensicherheit**. Dokumente können hochsensible personenbezogene, geschäftliche oder finanzielle Informationen enthalten. Sicherheitsanforderungen müssen daher bereits beim Systemdesign berücksichtigt werden und dürfen nicht nachträglich ergänzt werden.

---

# 2. Systemkontext

Das System wird als Docker-basierter Service betrieben.

Es stellt mindestens zwei unterschiedliche externe Schnittstellen bereit:

1. **Upload API**
    - für automatisierte Systeme wie Scanner oder andere Integrationen
    - keine interaktive Benutzeroberfläche notwendig
    - optimiert für die automatisierte Übertragung neuer Dokumente

2. **Web API / Web Application**
    - für menschliche Benutzer
    - Authentifizierung erforderlich
    - Zugriff auf Suche, Dokumentübersicht und Dokumentverwaltung

Die Dokuemte an sich werden auf Proton Drive gespeichert. über die Proton drive CLI wird automatisch das Dokument hochgeladen und heruntergeladen (dann in der web ui angezeigt oder heruntergeladen). Der Service hier hält das Dokument immer nur temporär zum anzeigen, runterladen oder wenn ich gerade verarbeitet wird!

Metadaten, Suchinformationen und weitere für die Anwendung notwendige Daten werden innerhalb der Anwendung verwaltet.

---

# 3. Akteure

## 3.1 Actor: Scanner

Der Actor **Scanner** bezeichnet ein automatisiertes Gerät oder einen Dienst, der Dokumente digitalisiert.

Ein typisches Gerät kann beispielsweise aus einem Dokumentenscanner und eigener IoT-Hardware bestehen.

Der Scanner erstellt vollständig digitalisierte PDF-Dokumente und überträgt diese anschließend an die Upload API.

Vor beziehungsweise während des Scanvorgangs kann der Benutzer am Scanner grundlegende Metadaten auswählen.

### Vom Scanner übermittelte Informationen

Mindestens:

- PDF-Dokument
- Bucket
- Dokumenttyp

Optional:

- Markierungen
- zusätzliche Metadaten

### Beispiele für Buckets

- Privat
- Unternehmen
- Studium

Buckets stellen eine logische Trennung unterschiedlicher Dokumentbereiche dar.

### Beispiele für Dokumenttypen

- Post
- Vertrag
- Rechnung
- Bescheid
- Abrechnung
- Sonstiges

### Beispiele für Markierungen

- Wichtig
- Todo

Eine Markierung ist zunächst keine Klassifizierung des Dokuments, sondern beschreibt den Bearbeitungszustand oder eine besondere Relevanz.

Beispiel:

Ein eingescannter Brief kann als `Todo` markiert werden, wenn der Benutzer ihn später noch bearbeiten oder beantworten muss.

---

## 3.2 Actor: User

Der Actor **User** ist ein authentifizierter menschlicher Benutzer des Systems.

Der Benutzer verwendet primär die Weboberfläche.

Er kann insbesondere:

- Dokumente suchen
- Dokumente filtern
- Dokumentdetails ansehen
- neue Dokumente erkennen
- Todo-Dokumente erkennen
- Metadaten überprüfen
- Klassifizierungen korrigieren
- Tags verwalten
- Dokumente abrufen

---

## 3.3 Actor: Server

Der Actor **Server** bezeichnet die automatisierten Backend-Komponenten des Systems.

Der Server übernimmt unter anderem:

- Annahme neuer Dokumente
- Validierung
- OCR
- Dokumentanalyse
- Klassifizierung
- Extraktion von Metadaten
- automatische Tag-Vergabe
- Indexierung
- Speicherung
- Abruf von Dokumenten
- Suchoperationen
- Sicherheitsfunktionen
- Authentifizierung und Autorisierung

---

## 3.4 Actor: Software Maintainer

Der **Software Maintainer** entwickelt, betreibt oder aktualisiert die Anwendung.

Seine Anforderungen betreffen insbesondere:

- Build-Prozess
- Deployment
- Konfiguration
- Secrets
- Datenbankmigrationen
- Docker Images
- CI/CD

---

# 4. Funktionale Anforderungen / User Stories

## UC01 – Automatisierter Dokumentenupload

**Als Scanner möchte ich ein vollständig eingescanntes PDF-Dokument zusammen mit grundlegenden Metadaten an den Server übertragen können, damit das Dokument anschließend ohne weitere manuelle Schritte verarbeitet und archiviert wird.**

Beim Upload werden mindestens folgende Informationen übertragen:

- PDF-Datei
- Bucket
- Dokumenttyp

Optional können übertragen werden:

- Markierungen wie `Todo`
- Markierung `Wichtig`
- weitere Metadaten

Nach erfolgreicher Annahme übernimmt der Server sämtliche weiteren Verarbeitungsschritte automatisiert.

### Erwarteter Ablauf

1. Scanner erstellt PDF.
2. Scanner authentifiziert sich gegenüber der Upload API.
3. Scanner übermittelt PDF und Metadaten.
4. Server validiert den Upload.
5. Server bestätigt die erfolgreiche Übernahme.
6. Dokument wird zur Verarbeitung vorgemerkt.
7. OCR wird durchgeführt.
8. Dokument wird analysiert.
9. Metadaten werden extrahiert.
10. Dokument wird klassifiziert.
11. Tags werden ermittelt.
12. Dokument wird dem richtigen Bucket zugeordnet.
13. Dokument wird dauerhaft gespeichert.
14. Dokument wird für Suche und Weboberfläche indexiert.

Ein Fehler innerhalb eines späteren Verarbeitungsschritts darf nicht dazu führen, dass das ursprünglich erfolgreich übertragene Dokument verloren geht.

---

## UC02 – Sichere Benutzeranmeldung

**Als User möchte ich mich sicher an der Weboberfläche authentifizieren können, damit ausschließlich autorisierte Personen Zugriff auf meine Dokumente und deren Inhalte erhalten.**

Die Authentifizierung soll mindestens auf folgenden Faktoren basieren:

- Benutzername
- Passwort
- zweiter Authentifizierungsfaktor

Für den zweiten Faktor sollen sichere Verfahren verwendet werden.

Beispiele:

- Passkey / WebAuthn
- Hardware Security Key
- TOTP

Die Authentifizierung ist eine sicherheitskritische Kernkomponente des Systems.

Ein einfacher Benutzername-/Passwort-Login ohne zusätzlichen Faktor ist für normale Benutzerzugriffe nicht ausreichend.

---

## UC03 – Volltextsuche und Filterung

**Als User möchte ich sämtliche Dokumente anhand ihres Inhalts durchsuchen können, damit ich Dokumente auch dann wiederfinde, wenn ich weder Dateiname noch genaue Klassifizierung kenne.**

Die Suche muss insbesondere OCR-erkannten Dokumentinhalt berücksichtigen.

Beispiel:

Eine Suche nach

`Fahrzeugversicherung`

soll ein Dokument finden können, in dem dieser Begriff lediglich innerhalb des eingescannten Dokuments vorkommt.

Zusätzlich zur Volltextsuche sollen Filter möglich sein.

Mindestens:

- Bucket
- Dokumenttyp
- Tags
- Markierungen
- Scan-/Upload-Datum
- erkanntes Dokumentdatum
- Status
- Absender beziehungsweise erkannte Organisation

Kombinationen verschiedener Filter müssen möglich sein.

Beispiel:

> Alle Rechnungen aus dem Bucket `Privat`, die sich auf ein Fahrzeug beziehen und aus dem Jahr 2026 stammen.

---

## UC04 – Dokumentübersicht

**Als User möchte ich unmittelbar erkennen können, welche Dokumente neu sind und bei welchen Dokumenten noch eine Aktion erforderlich ist, damit eingehende Post nicht im Archiv untergeht.**

Die zentrale Dokumentübersicht soll deshalb mindestens unterschiedliche Zustände sichtbar machen.

Dazu gehören insbesondere:

- neue Dokumente
- ungelesene Dokumente
- Todo-Dokumente
- wichtige Dokumente
- kürzlich hinzugefügte Dokumente

Der Benutzer soll dadurch die Weboberfläche nicht nur als Archiv, sondern auch als Eingangskorb für neue Dokumente verwenden können.

---

## UC05 – Automatische Dokumentanalyse

**Als Server möchte ich neu eingegangene Dokumente automatisch analysieren und klassifizieren, damit der Benutzer möglichst wenig manuelle Sortierarbeit durchführen muss.**

Nach der OCR-Verarbeitung soll der Server versuchen, Informationen über das Dokument zu bestimmen.

Dazu können gehören:

- aussagekräftiger Dokumenttitel
- Absender
- Organisation
- Dokumentdatum
- Dokumenttyp
- relevante Tags
- thematische Zuordnung
- mögliche Zusammengehörigkeit mit anderen Dokumenten

Die automatische Analyse darf bestehende Dokumente und frühere Klassifizierungen berücksichtigen.

---

## UC06 – Wiederkehrende Dokumente erkennen

**Als User möchte ich, dass regelmäßig wiederkehrende Dokumente als zusammengehörig erkannt werden, damit ich Dokumentserien einfach nachvollziehen kann.**

Beispiele:

- monatliche Gehaltsabrechnungen
- jährliche Versicherungsunterlagen
- Mobilfunkrechnungen
- Stromabrechnungen
- Konto- oder Depotauszüge
- Rechnungen zu einem bestimmten Fahrzeug
- Dokumente zu einem bestimmten Vertrag

Beispielsweise sollen mehrere Gehaltsabrechnungen nicht lediglich unabhängig voneinander mit `Gehalt` getaggt werden.

Das System soll erkennen können, dass es sich um eine zusammengehörige Dokumentserie handelt.

Eine mögliche Darstellung wäre beispielsweise:

```text
Gehaltsabrechnungen
├── Januar 2026
├── Februar 2026
├── März 2026
├── April 2026
└── ...
```

Wie diese Zusammengehörigkeit technisch modelliert wird, wird im späteren Architektur- und Datenmodell festgelegt.

---

## UC07 – Automatische Tag-Vergabe

**Als Server möchte ich Dokumenten auf Basis ihres Inhalts automatisch geeignete Tags zuordnen, damit Dokumente ohne umfangreiche manuelle Pflege strukturiert werden.**

Beispiele:

```text
Rechnung
Fahrzeug
Versicherung
Motorrad
Steuer
Studium
RettLog
```

Bereits vorhandene Tags sollen bevorzugt wiederverwendet werden.

Das System soll vermeiden, für semantisch gleiche Inhalte ständig neue ähnliche Tags zu erzeugen.

Beispiel:

Nicht:

```text
Auto
KFZ
Kfz
PKW
Automobil
Kraftfahrzeug
```

wenn diese Kategorien eigentlich dasselbe ausdrücken sollen.

---

## UC08 – Manuelle Korrektur automatischer Erkennung

**Als User möchte ich automatisch erkannte Informationen ändern können, damit Fehler bei OCR, Klassifizierung oder automatischer Analyse korrigiert werden können.**

Änderbar sollen mindestens sein:

- Titel
- Dokumentdatum
- Dokumenttyp
- Bucket
- Tags
- Absender
- Markierungen
- erkannte Zusammengehörigkeit

Automatisch erzeugte Informationen dürfen niemals als unumstößlich betrachtet werden.

---

## UC09 – Dokumentstatus und Todo-Verwaltung

**Als User möchte ich Dokumente als erledigt beziehungsweise offen markieren können, damit ich das System gleichzeitig als digitalen Posteingang verwenden kann.**

Mindestens folgende Zustände sollen berücksichtigt werden können:

```text
Neu
Todo
Erledigt
```

Zusätzlich kann unabhängig davon eine Markierung wie

```text
Wichtig
```

existieren.

Beispiel:

```text
Bescheid Finanzamt

Status: Todo
Markierung: Wichtig
```

Nach Bearbeitung:

```text
Status: Erledigt
Markierung: Wichtig
```

---

# 5. Deployment und Betrieb

## UC10 – Docker Deployment

**Als Software Maintainer möchte ich das gesamte System über Docker betreiben können, damit Installation, Updates und Betrieb reproduzierbar und möglichst einfach sind.**

Das Projekt soll ein produktionsfähiges Docker Image bereitstellen.

Das Image soll über GitHub Container Registry veröffentlicht werden.

Beispiel:

```text
ghcr.io/<organisation>/<project>:latest
```

Zusätzlich sollen versionierte Images verfügbar sein.

Beispiel:

```text
ghcr.io/<organisation>/<project>:1.4.2
```

---

## UC11 – Copy-Paste-fähiges Docker Compose

**Als Software Maintainer möchte ich eine vollständige Beispielkonfiguration erhalten, damit eine neue Instanz mit möglichst wenig manueller Konfiguration betrieben werden kann.**

Das Repository soll deshalb mindestens eine

```text
docker-compose.yml
```

beziehungsweise

```text
compose.yml
```

enthalten.

Diese soll alle zwingend benötigten Komponenten enthalten.

Beispielsweise:

```text
Application
Database
Worker
```

sowie gegebenenfalls weitere notwendige Services.

Nach Einrichtung der Secrets soll ein Deployment grundsätzlich mit einem Befehl möglich sein:

```bash
docker compose up -d
```

---

## UC12 – CI/CD und Container Build

**Als Software Maintainer möchte ich, dass bei Releases automatisiert ein fertiges Docker Image erstellt und veröffentlicht wird, damit Releases reproduzierbar und ohne manuellen Buildprozess bereitgestellt werden.**

GitHub Actions soll mindestens:

1. Source Code auschecken
2. Tests ausführen
3. Anwendung bauen
4. Docker Image bauen
5. Image korrekt taggen
6. Image in GHCR veröffentlichen

Fehlgeschlagene Tests müssen verhindern, dass ein Release-Image veröffentlicht wird.

---

## UC13 – Sichere Secret-Verwaltung

**Als Software Maintainer möchte ich Secrets außerhalb von Source Code und normalen Konfigurationsdateien verwalten können, damit Zugangsdaten nicht versehentlich veröffentlicht werden.**

Secrets dürfen insbesondere nicht enthalten sein in:

- Git Repository
- Docker Image
- öffentlich dokumentierten Compose-Dateien
- Application Logs

Secrets für externe Dienste sollen über Dateien beziehungsweise Docker Secrets eingebunden werden können.

Beispiel:

```yaml
secrets:
  storage_credentials:
    file: ./secrets/storage_credentials
```

Die Anwendung soll Secrets bevorzugt über einen entsprechenden `*_FILE`-Mechanismus einlesen können.

Beispiel:

```text
STORAGE_PASSWORD_FILE=/run/secrets/storage_password
```

anstatt:

```text
STORAGE_PASSWORD=my-secret-password
```

---

# 6. Qualitätsanforderungen

## QR01 – Einfachheit der Benutzeroberfläche

Die Weboberfläche soll:

- übersichtlich
- schnell
- professionell
- konsistent
- responsiv
- möglichst selbsterklärend

sein.

Der Hauptanwendungsfall ist:

> Dokument finden oder neue Dokumente bearbeiten.

Die Oberfläche soll daher keine unnötige administrative Komplexität aufweisen.

Die wichtigsten Bereiche sollen unmittelbar erreichbar sein.

Beispielsweise:

```text
Inbox
Dokumente
Todos
Suche
Tags
Einstellungen
```

---

# 7. Sicherheitsanforderungen

Datensicherheit gehört zu den höchsten Prioritäten des Projekts.

Das System muss davon ausgehen, dass gespeicherte Dokumente besonders sensible Informationen enthalten können.

Dazu gehören beispielsweise:

- Vertragsinformationen
- Adressen
- Finanzinformationen
- Steuerinformationen
- geschäftliche Unterlagen
- personenbezogene Daten

Security muss deshalb Bestandteil der Architektur sein und darf nicht ausschließlich auf die Absicherung des äußeren Webservers reduziert werden.

---

## SEC01 – Keine ungeschützten Dokumentinhalte in der Datenbank

OCR-Inhalte sind als sensible Daten zu behandeln.

Der vollständige OCR-Text eines Dokuments darf nicht ohne angemessenen Schutz dauerhaft im Klartext in der Datenbank gespeichert werden.

Dabei muss allerdings berücksichtigt werden, dass gleichzeitig eine performante Volltextsuche über Dokumentinhalte erforderlich ist.

Für diesen Zielkonflikt muss im Security- und Architekturkonzept eine explizite Lösung entwickelt werden.

Mögliche Lösungsansätze sollen später hinsichtlich Sicherheit, Suchbarkeit und Komplexität bewertet werden.

---

## SEC02 – Sichere Authentifizierung

Benutzerkonten müssen gegen Accountübernahme geschützt werden.

Mindestens erforderlich:

- sicherer Passwort-Hashing-Algorithmus
- starke Passwortregeln
- Multi-Faktor-Authentifizierung
- Schutz gegen Brute-Force-Angriffe
- Rate Limiting
- sichere Session-Verwaltung
- sichere Cookies
- Session-Ablauf
- Logout und Session-Invalidierung

Passwörter dürfen niemals:

- im Klartext gespeichert
- reversibel verschlüsselt
- geloggt

werden.

---

## SEC03 – Multi-Faktor-Authentifizierung

Für den Zugriff auf die Weboberfläche muss Multi-Faktor-Authentifizierung unterstützt werden.

Bevorzugt:

1. WebAuthn / Passkeys / Hardware Security Keys

Alternativ beziehungsweise ergänzend:

2. TOTP

Unsichere zweite Faktoren wie SMS sollen nicht als primäres MFA-Verfahren vorgesehen werden.

---

## SEC04 – Trennung der Upload API von Benutzerzugriffen

Scanner und andere automatisierte Systeme dürfen keine normalen Benutzerzugänge verwenden.

Die Upload API benötigt ein eigenes Authentifizierungs- und Berechtigungskonzept.

Ein Scanner soll ausschließlich die für ihn notwendigen Aktionen ausführen können.

Beispielsweise:

```text
Scanner:
✓ Dokument hochladen
✓ Upload-Status abrufen

✗ Dokumente suchen
✗ Dokumente herunterladen
✗ Benutzer verwalten
✗ Einstellungen ändern
```

Damit soll ein kompromittiertes Scan-Gerät nicht automatisch Zugriff auf das Dokumentenarchiv erhalten.

---

## SEC05 – Least Privilege

Alle Systemkomponenten sollen nach dem Least-Privilege-Prinzip betrieben werden.

Jede Komponente erhält ausschließlich die Berechtigungen, die sie für ihre Aufgabe benötigt.

Dies betrifft insbesondere:

- Scanner
- Web Application
- Worker
- Datenbank
- Storage-Zugriff
- Administrationsfunktionen

---

## SEC06 – Transportverschlüsselung

Sämtliche externe Kommunikation muss verschlüsselt erfolgen.

HTTP ohne TLS darf für produktive externe Zugriffe nicht verwendet werden.

Dies gilt insbesondere für:

- Weboberfläche
- Upload API
- Storage-Kommunikation
- externe Integrationen

---

## SEC07 – Schutz temporärer Dokumente

Während der Verarbeitung können temporäre Dateien entstehen.

Beispielsweise bei:

- Upload
- OCR
- PDF-Konvertierung
- Storage-Upload

Temporäre Dokumente müssen:

- kontrolliert gespeichert
- vor unberechtigtem Zugriff geschützt
- nach erfolgreicher Verarbeitung zuverlässig gelöscht

werden.

---

## SEC08 – Keine sensiblen Inhalte in Logs

Logs dürfen keine vollständigen Dokumentinhalte enthalten.

Insbesondere dürfen nicht protokolliert werden:

- Passwörter
- Authentifizierungstoken
- vollständige OCR-Inhalte
- Storage-Credentials
- Session Tokens
- private Schlüssel

---

## SEC09 – Sichere Standardkonfiguration

Eine Standardinstallation soll möglichst bereits sicher konfiguriert sein.

Das Projekt folgt damit dem Prinzip:

> Secure by default.

Unsichere Konfigurationen sollen nicht erforderlich sein, um eine Instanz schnell in Betrieb zu nehmen.

---

# 8. Zuverlässigkeitsanforderungen

## REL01 – Kein Dokumentverlust

Ein Dokument darf nach erfolgreicher Bestätigung des Uploads nicht verloren gehen.

Schlägt ein späterer Verarbeitungsschritt fehl, muss das Dokument weiterhin vorhanden sein und erneut verarbeitet werden können.

---

## REL02 – Wiederholbare Verarbeitung

Verarbeitungsschritte sollen soweit möglich idempotent gestaltet werden.

Ein Dokument darf beispielsweise nach einem Neustart oder Worker-Absturz erneut verarbeitet werden können, ohne dass dadurch unbeabsichtigt mehrere Dokumenteinträge entstehen.

---

## REL03 – Fehlerzustände sichtbar machen

Dokumente mit fehlgeschlagener Verarbeitung müssen für den Benutzer beziehungsweise Administrator sichtbar sein.

Beispiel:

```text
Processing failed:
OCR processing failed
```

Das Dokument darf dadurch nicht verschwinden.

---

# 9. Erweiterbarkeit

Die Architektur soll spätere Integrationen ermöglichen, ohne dass die Kernanwendung grundlegend umgebaut werden muss.

Denkbare zukünftige Integrationen sind beispielsweise:

- unterschiedliche Storage-Backends
- Proton Drive
- S3-kompatibler Storage
- lokale Filesysteme
- E-Mail-Import
- zusätzliche Scanner
- Mobile Upload
- Webhooks
- externe Automatisierungen
- lokale KI-Modelle
- unterschiedliche OCR-Systeme

Diese Funktionen müssen nicht Bestandteil der ersten Version sein.

Die Architektur sollte ihre spätere Integration jedoch nicht unnötig erschweren.

---

# 10. Abgrenzung der ersten Version

Das Ziel der ersten Version ist ausdrücklich **nicht**, sämtliche Funktionen bestehender Enterprise-DMS-Systeme nachzubauen.

Der Kernumfang soll zunächst sein:

```text
Dokument empfangen
        ↓
sicher übernehmen
        ↓
OCR
        ↓
analysieren
        ↓
klassifizieren
        ↓
ablegen
        ↓
indexieren
        ↓
finden
        ↓
bearbeiten
```

Die Anwendung soll insbesondere für einen kleinen Benutzerkreis beziehungsweise zunächst einen einzelnen Benutzer optimiert sein.

Komplexe Funktionen wie umfangreiche Mandantenfähigkeit, öffentliche Freigaben oder Enterprise-Workflow-Systeme gehören zunächst nicht zum Kernumfang.

---

# 11. Kernprinzipien

Das Projekt folgt insbesondere folgenden Prinzipien:

### Security by Design

Sicherheit wird bereits bei Architektur und Datenmodell berücksichtigt.

### Secure by Default

Eine Standardinstallation soll möglichst sicher sein.

### Automation First

Manuelle Arbeit bei Dokumentimport und Klassifizierung soll minimiert werden.

### Human Override

Automatisch erkannte Informationen müssen durch den Benutzer korrigierbar bleiben.

### Simple UI

Die Anwendung soll sich auf Dokumente, Suche und Bearbeitung konzentrieren.

### Open Source

Das Projekt soll öffentlich nachvollziehbar, selbst hostbar und unabhängig von einem zentralen Anbieter betreibbar sein.

### Portable Deployment

Eine Instanz soll über Docker mit möglichst wenig Aufwand reproduzierbar bereitgestellt werden können.

# 12. Systemarchitektur

Die Anwendung wird als **modularer Monolith** entwickelt. Frontend und Backend sind im Repository getrennt, werden aber gemeinsam in einem Docker-Image ausgeliefert. Die Architektur ist für eine privat betriebene Einzelbenutzerinstanz ausgelegt.

## 12.1. Technologie-Stack

| Bereich | Technologie |
|---|---|
| Frontend | React + TypeScript + Vite |
| UI | Tailwind CSS + shadcn/ui |
| Routing | React Router |
| API-Zugriff und clientseitiger Datenzustand | TanStack Query |
| PDF-Anzeige | PDF.js |
| Backend | Python + Django |
| REST-API | Django Ninja |
| Datenbank | PostgreSQL |
| ORM und Migrationen | Django ORM |
| Hintergrundverarbeitung | Python-Worker mit PostgreSQL-basierter Job-Queue |
| OCR | OCRmyPDF + Tesseract |
| PDF-Verarbeitung | pikepdf |
| Dokumentenspeicher | Proton Drive über die Proton Drive CLI |
| Deployment | Docker Multi-Stage Build |

## 12.2. Komponenten und Zuständigkeiten

**Frontend:** Die React-Anwendung stellt Dokumentverwaltung, Suche, Bearbeitung und PDF-Anzeige bereit. Sie kommuniziert ausschließlich über die REST-API mit dem Backend. TypeScript-Typen werden aus dessen OpenAPI-Spezifikation generiert.

**Backend:** Django enthält die Geschäftslogik, Authentifizierung, Upload-API, Dokumentverwaltung und Suche. Django Ninja stellt die REST-Endpunkte bereit. Das Frontend verwendet serverseitige Sessions mit geschützten Cookies und CSRF-Schutz.

**Verarbeitungsworker:** Ein separater Python-Prozess übernimmt OCR, Metadatenextraktion und Klassifizierung. Er verwendet dieselben Backend-Module wie die API und bezieht seine Aufträge aus einer persistenten Job-Tabelle in PostgreSQL.

**Storage-Anbindung:** Ein internes Storage-Interface kapselt die Proton Drive CLI. Proton Drive speichert die Dokumentdateien dauerhaft. Der Service hält Dateien ausschließlich temporär zur Verarbeitung, Anzeige oder Auslieferung eines Downloads.

**Datenbank:** PostgreSQL speichert Metadaten, Tags, Aufgaben, Storage-Referenzen, Verarbeitungsaufträge und Suchdaten. Das Verschlüsselungs- und Suchmodell wird gesondert festgelegt.

## 12.3. Codeorganisation

Das gemeinsame Repository enthält zwei Hauptbereiche:

- `frontend/`: React-Anwendung mit Komponenten, Ansichten und API-Client.
- `backend/`: Django-Projekt mit Modulen für Accounts, Dokumente, Import, Verarbeitung, Suche und Storage.

API und Worker greifen auf gemeinsame Services im Backend zu. OCR, Storage und optionale KI-Analyse werden über interne Schnittstellen angebunden.

## 12.4. Deployment

Der Docker-Build kompiliert zunächst das Frontend und übernimmt dessen statische Dateien anschließend in das Python-Image. Zur Laufzeit wird kein Node.js benötigt.

Ein Application-Container enthält den Frontend-Build, die Django-API, den Python-Worker, die OCR-Werkzeuge und die Proton Drive CLI. Webserver und Worker laufen als getrennte Prozesse unter einem Prozessmanager.

PostgreSQL wird als separater Container betrieben. Der vorhandene Reverse Proxy stellt die Anwendung über HTTPS bereit; Frontend und API verwenden dieselbe Origin.