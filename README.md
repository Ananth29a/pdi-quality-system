# Factory PDI & Dispatch Clearance System

A Django implementation of four connected features from the *Factory Operations & Quality Control System* BRD (Zapuza Technologies LLP). Together they form one flow: **gauge calibration -> inspector checklist -> supervisor review -> shipping interlock**.

All business rules live in the **model layer** (`quality/models.py`), so they are enforced on the server whether a record is created through the admin, a script or the tests. The UI only displays results.

## Features built

### 1. Gauge calibration register (BRD 2.3)
- `Gauge` records have a calibration expiry date.
- A measurement is rejected if no gauge is selected or the gauge is expired ("Expired Tool Calibration").
- A gauge is valid through its expiry date and invalid the day after.

### 2. PDI template and inspector checklist (BRD 4, 5)
- `ChecklistItem` defines a step with min/max tolerance, a numeric or visual pass/fail type, and a **CTQ** flag.
- Tolerance check is `min <= value <= max`, using `Decimal`, so boundary values (for example exactly 25.05) are exact.
- **Locked step-by-step entry:** a step cannot be recorded until every earlier step in its section is recorded.
- **CTQ freeze:** an out-of-tolerance CTQ entry immediately sets the checklist to `FAILED` and blocks further entries.
- Non-CTQ failures are recorded and the inspector can continue; the supervisor decides later.
- Entries are **write-once**: no edits or deletes, and a closed checklist accepts nothing.
- Template validation: `min <= max`, and numeric items need at least one limit.
- Each entry stores who entered it and when.

### 3. Supervisor review (BRD 6)
Decisions: **Approved**, **Conditional Concession**, **Quarantine**.
- Approve only if the checklist is complete and nothing is out of tolerance.
- Concession only for non-CTQ failures, with a mandatory reason code and a non-blank justification. CTQ failures can never be conceded.
- Quarantine is allowed on any submitted or CTQ-frozen checklist (status `QUARANTINED`).
- Needs the `can_review_pdi` permission; the inspector cannot review their own checklist.
- One review per checklist; reviews cannot be edited or deleted. Concession lots stay flagged (`CONCESSION`).

### 4. Shipping interlock / gate pass (BRD 10)
- A `GatePass` can only be issued if the checklist is `APPROVED` or `CONCESSION`.
- Otherwise it is refused with the exact reason, for example `Shipping Interlock: Lot is quarantined in the Rejection Holding Area...`.
- Needs the `can_issue_gate_pass` permission; one gate pass per lot; passes are permanent.

### Admin interface
Django admin is the UI. It stamps the logged-in user on new records, computes results instead of trusting typed input, makes entries, reviews and gate passes view-only after creation, and has a "Submit selected checklists" action.

## Not built (out of scope, by choice)
QR code scanning, offline sync, mandatory defect photos, process hold timers, raw material intake / MTC, part lineage traceability, MRB dispositions (rework, scrap, RTV), template revisions (Rev A/B) and the shift handover log.
The shipping interlock checks **only the PDI condition**; operation routing and hold-time conditions (BRD 10, conditions a and b) are not modeled.

## Tech stack
Python 3.12+ and Django 6.x (SQLite).

## How to run

```bash
git clone https://github.com/Ananth29a/pdi-quality-system.git
cd <repo-name>

python -m venv venv
# Windows:        venv\Scripts\activate
# Mac / Linux:    source venv/bin/activate

pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```
Open http://127.0.0.1:8000/admin and log in.

## How to run the tests

```bash
python manage.py test quality
```
50 tests across `tests.py` (tolerance, gauge, step lock, CTQ, read-only), `test_review.py`, `test_gatepass.py` and `test_admin.py` (admin cannot bypass the rules).

## Quick walkthrough in the admin
1. **Gauges:** add `VC-104` (expiry `2027-12-31`) and `VC-OLD` (expiry `2025-01-01`).
2. **Checklist items:** `4.10 Shaft diameter` (min 24.98, max 25.05) and `4.20 Thread pitch` (min 1.00, max 1.10, tick CTQ).
3. **PDI checklists:** add `LOT-1`.
4. **Inspector entries:** 4.20 first -> blocked (step lock); 4.10 with `VC-OLD` -> blocked (expired); 4.10 = `25.06` (fail, saved); 4.20 = `1.05` (pass).
5. Run **Submit selected checklists** on `LOT-1` -> `FAILED`.
6. Create a second user with Staff + Superuser status (a reviewer cannot be the inspector). **Reviews:** concession on `LOT-1` needs a reason code and justification.
7. **Gate passes:** `LOT-1` is issued (flagged as released on concession). A quarantined lot is refused.

## Data model
`Gauge`; `ChecklistItem` (template); `PDIChecklist` (one inspection run, status `IN_PROGRESS / PASSED / FAILED / APPROVED / CONCESSION / QUARANTINED`); `InspectorEntry` (one measurement, write-once); `Review` (one per checklist); `GatePass` (one per checklist).

## Design decisions and known limits
- Rules run in `clean()` and `save()` (via `full_clean()`), not only in forms. `QuerySet.update()` and `bulk_create()` skip `save()`, so records must be written through `.save()` / `.create()`.
- `DecimalField` is used for all measurements and limits to avoid floating point errors at tolerance boundaries.
- `asset_serial_number` is not unique, so a reworked lot can be inspected again.
- A failed CTQ step freezes the checklist; a non-CTQ failure does not block the inspector.
- Roles are Django permissions (`can_review_pdi`, `can_issue_gate_pass`); there is no custom login UI.
- Templates are not locked after release and have no revision history.
- Settings are development defaults (SQLite, `DEBUG=True`), not production-ready.
