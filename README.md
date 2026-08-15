# ResQNet

**Intelligent Disaster Response & Relief Management Platform**

> Report. Respond. Recover.

ResQNet manages the complete disaster-relief lifecycle — from a citizen's damage
report through triage, field verification and compensation, to a tracked payout —
connecting **citizens**, **field officers** and **government administrators**.

Built as an academic project. A Django monolith, deliberately.

---

## The problem it addresses

After a disaster, the bottleneck is rarely collecting reports. It is triage:
deciding which of several thousand reports describes a family trapped under a
collapsed roof, and which describes a damaged boundary wall that can wait a week.
Done by hand that decision is slow, inconsistent, and hard to justify afterwards.

ResQNet scores urgency against **published rules**, dispatches the nearest
available officer, bases relief on **verified** findings rather than claims, and
records every significant action in an append-only history.

---

## Lifecycle

```
Citizen reports damage
        ↓
Smart triage  ──────────  rule-based score → LOW / MEDIUM / HIGH / CRITICAL
        ↓
Automatic officer assignment  ──  availability, zone, distance, workload
        ↓
Field inspection  ────────  graded severity per damage type, verified photos
        ↓
Compensation calculation  ─  configurable relief matrix, itemised breakdown
        ↓
Admin approval  ──────────  approve / reject / request re-inspection
        ↓
Payout tracking  ─────────  pending → initiated → completed
```

Status is never assigned directly. Every change passes through a state machine
([`reports/states.py`](reports/states.py)) that enforces the rules structurally:

- An administrator **cannot** approve an uninspected case.
- A payout **cannot** complete before approval.
- A citizen **cannot** modify a finalised inspection.
- An officer **cannot** edit another officer's inspection.

---

## Architecture

```
Models  →  Services / business logic  →  Serializers / validation  →  Views / API
```

Business logic lives in `services.py` modules, not in views. That keeps rules
testable in isolation and reusable from anywhere.

| App | Responsibility |
|---|---|
| `accounts` | Custom user model, three roles, officer profiles, auth, permissions |
| `reports` | Citizen damage reports, evidence photos, the case state machine |
| `triage` | Rule-based scoring and severity classification |
| `dispatch` | Automatic officer assignment (Haversine distance) |
| `inspections` | Field verification, graded severity, officer sign-off |
| `compensation` | Configurable relief matrix, itemised claims |
| `approvals` | Administrator decisions and payout tracking |
| `audit` | Append-only audit trail |
| `analytics` | Aggregated read-only statistics |
| `web` | Server-rendered public site and the three role portals |

**Stack:** Django 5.2 · Django REST Framework · PostgreSQL (SQLite in dev) ·
IndexedDB · Leaflet.js

No microservices, no Celery, no Redis, no ML. Each piece of infrastructure that
is present is there because a requirement demanded it.

---

## Design decisions worth knowing

**Triage is rule-based, not machine-learned.** When a citizen asks why their case
ranked below their neighbour's, a public relief programme has to be able to
answer. "The model decided" is not an acceptable answer; a sum of published rules
is. Weights live in the database so an administrator can retune them mid-event
without a deploy, and every score stores the itemised breakdown that produced it.

**Ownership is enforced by queryset filtering, not permission checks.** A citizen
requesting another citizen's report gets a `404`, because that row was never in
their queryset. There is no code path where a forgotten check leaks data.

**Compensation uses `Decimal`, never `float`.** Binary floating point cannot
represent `0.1` exactly and the error compounds across a payout run.

**Login throttling counts failed attempts only.** Counting every request means a
correct password consumes quota, so users behind one shared connection lock each
other out while an attacker is barely slowed.

**The audit trail is append-only at the application layer.** The model refuses
updates and deletes, and no route exposes `PUT` or `DELETE`. It is a standard
database table, so a DBA with direct SQL access could still alter it — it is
tamper-evident, *not* cryptographically immutable.

---

## Offline-first

Relief work happens where the network does not.

**Citizen:** a report filled in with no signal is stored in IndexedDB and sent
automatically on reconnection.

**Field officer:** assignments are downloaded *before* entering the zone, the
inspection is completed offline, and it syncs later.

Both paths carry a client-generated **idempotency key**, created once when the
user pressed Save and reused on every retry. A replayed submission is recognised
and returns the original case instead of creating a duplicate — which in a relief
platform would mean duplicate inspections and duplicate payments.

---

## Getting started

**Requirements:** Python 3.12+

```bash
git clone https://github.com/akmalalaam30/resQnet.git
cd resQnet

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

Create your environment file:

```bash
cp .env.example .env           # copy .env.example on Windows
```

Generate a secret key and paste it into `.env`:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Then:

```bash
python manage.py migrate
python manage.py seed_demo     # optional: demo accounts + cases
python manage.py runserver
```

Open **http://127.0.0.1:8000/**

### Demo accounts

`seed_demo` builds ten cases spread across every stage of the lifecycle, created
through the real services — so the triage scores, dispatch distances and
compensation totals are genuinely computed, not fabricated.

| Username | Role |
|---|---|
| `admin` | Administrator |
| `officer.rao` | Field officer (Kollam) |
| `citizen.das` | Citizen |

Password for all demo accounts: `ResQNet@2026` — local development only.

Rebuild at any time with `python manage.py seed_demo --reset`.

---

## Database

SQLite by default so the project runs with nothing to install. PostgreSQL is the
intended database; switching is a one-line change in `.env`:

```
DATABASE_URL=postgres://user:password@localhost:5432/resqnet
```

---

## Tests

```bash
python manage.py test
```

**190 tests** covering authentication, authorisation and object-level ownership,
report validation, the state machine, triage scoring, dispatch selection,
inspection permissions, compensation arithmetic, the approval workflow, audit
immutability, offline sync idempotency, and page rendering.

---

## API

| Endpoint | Purpose |
|---|---|
| `/api/health/` | Service and database liveness |
| `/api/auth/` | Registration, login, logout, officer management |
| `/api/reports/` | Damage reports, evidence, offline sync |
| `/api/triage/` | Scoring rules, thresholds, results |
| `/api/dispatch/` | Assignments, offline package, manual assignment |
| `/api/inspections/` | Field inspections, sign-off, offline sync |
| `/api/compensation/` | Relief matrix, claims, breakdowns |
| `/api/approvals/` | Approve, reject, request review, payouts |
| `/api/analytics/` | Statistics, heatmap, officer workload |
| `/api/audit/` | Audit trail (read-only) |

---

## Important limitations

This is an academic project.

- **Not connected to emergency services.** Filing a report does not summon help.
  In a life-threatening emergency, call your national emergency number.
- **Payout processing is simulated.** No banking or treasury system is connected
  and no money moves.
- **Not an official government system.** It carries no authority to award relief.

---

## Licence

Academic project. Not licensed for operational use.
