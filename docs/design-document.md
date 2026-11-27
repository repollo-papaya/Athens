# ARC Rocket Design Optimizer: Design Document

Nov 17, 2026 · @Aldo Sanchez · Revised Nov 27, 2026

> **Revision, Nov 27, 2026.** Added [Categories of information](#categories-of-information),
> which takes the team's three categories (launch conditions, fixed rocket components,
> variants to optimize) and adds rules, run settings, versioning and uncertainty.
> [Data contracts](#data-contracts), the Firestore collections and the open questions
> were updated to match. Firestore is now set up (project `athens-2509f`) and passes a
> read/write connection test.

## Purpose and goals

The app recommends which rocket to build for a given set of launch conditions. It runs
hundreds of simulations, ranks designs by expected competition score, and returns the top
options with a robustness estimate. It replaces the current manual loop: simulate a few
options in RocketSim, pick the best, build, test.

Context from the 2027 ARC parameters: qualification flights
target 800 ft, Finals targets fall between 775 and 825 ft, duration target is 37 to 40 s,
and the payload is two large eggs (55 to 63 g). In earlier years the score was altitude
error plus 4 times duration error, lower is better. Confirm the 2027 formula in the
official rules before hard-coding it.

Proposed success measures (the team should set the final numbers):

- The sim reproduces at least 3 real flights within a tolerance the team agrees on (for
  example 3% on apogee) before the optimizer is trusted.
- A study of several hundred simulations returns in under 5 minutes.
- Over a season, real flights of recommended designs land closer to target than
  hand-picked ones.

## Scope and non-goals

Version 1 recommends designs for single-stage, egg-lofting rockets and nothing more.

In scope:

- Motors from the approved list only.
- Launch conditions: temperature, wind and gusts, field elevation, pressure, target altitude.
- Ranked recommendations with a robustness estimate, not just a single best design.
- Logging real flights and recalibrating the simulator from them.
- AI that turns free-text launch conditions into a request the user reviews and confirms.

Out of scope:

- Safety approval. The rules, the team advisor and the range safety officer decide whether
  a rocket is legal and safe to fly. The app only ranks designs.
- Replacing test flights. The app narrows the field to 2 or 3 designs worth building and flying.
- Building a simulation model from photos. Dimensions and masses come from measurements or
  a design file.
- Running code written by the AI. The worker only runs code the team wrote and tested.
- Multi-stage or high-power rockets.
- Live use at the field without signal. Precomputed design cards cover that case.

## Users and key flows

The users are the student team (3 to 10 students, per the 2027 parameters) and the adult
advisor. Everyone signs in with an allowed Google account.

1. Sign in.
2. Add a rocket once: dimensions, masses, parachute, candidate motors, or an uploaded
   OpenRocket file. This saves a rocket version and its default variant ranges
   (see [Categories of information](#categories-of-information)).
3. Describe launch conditions in plain text or fill in the form. A weather forecast can
   pre-fill the form. The AI parser fills the form from text, and the user reviews and edits it.
4. Submit. The app saves a job and shows its status: queued, running, done or failed.
5. Read the results: ranked designs, expected apogee and duration with their spread, the
   score distribution, and a build sheet for the winner.
6. After a real flight, log apogee, duration, weather, the rocket version and the motor
   used. The app uses it to recalibrate the simulator.

## System architecture

Firestore is the only meeting point between the web app and the worker.

```
                          ┌──────────── Firebase ────────────┐
                          │   Auth (Google sign-in)          │
┌──────────────┐  writes  │  ┌────────────────────────────┐  │  polls jobs   ┌───────────────────┐
│ Web app      │  jobs,   │  │ Firestore                  │  │  writes       │ Worker            │
│ GitHub Pages │─────────▶│  │ rules, rockets, jobs,      │◀─┼───────────────│ VM or laptop      │
│ form, status,│◀─────────│  │ results, flights,          │  │  results      │ OpenRocket +      │
│ results      │  reads   │  │ calibration                │  │               │ optimizer         │
└──────┬───────┘  results │  └────────────────────────────┘  │               │ no inbound ports  │
       │                  └──────────────────────────────────┘               └───────────────────┘
       │ free-text conditions
       ▼
┌──────────────┐  text + schema    ┌────────────┐
│ Parse        │──────────────────▶│ Claude API │
│ function     │◀──────────────────│            │
│ holds API key│  conditions JSON  └────────────┘
└──────────────┘
```

The browser writes a job to Firestore and listens for the result. The worker polls
Firestore for pending jobs, runs the simulations and optimizer, and writes the ranked
results back. The worker accepts no inbound connections, so there is no public endpoint
to secure. Free text goes through a separate parse function that holds the Claude API
key. Where that function runs (a Cloud Function, or a job type handled by the worker) is
an open decision.

## Categories of information

The app's inputs fall into separate categories. Each one changes at a different rate, is
set by different people, and needs different checks. Keeping them apart means each can be
stored, validated and secured on its own. For example, the AI parser can only ever write
launch conditions, which is the safety boundary described in [AI input mapping](#ai-input-mapping).

The team defined three categories:

1. **Launch conditions.** These change each time the user wants to run an optimization.
2. **Fixed rocket components.** These do not change. They are stored once and used for
   each simulation run.
3. **Variant options and ranges.** The options the system will consider when it optimizes
   the rocket.

In optimization terms these are the *environment*, the *fixed parameters* and the
*decision variables*. The full set, with the additions recommended below:

| # | Category | What it holds | Changes | Set by |
|---|---|---|---|---|
| 0 | Competition rules | Score formula, target windows, approved motors, minimum stability, size and mass limits | Once a year | Team, from the official rules |
| 1 | Launch conditions | **Weather** (temperature, wind, gusts, pressure, elevation, launch angle) and **target** (apogee, duration window) | Every run | User, forecast or AI parser; user confirms |
| 2 | Fixed rocket components | Dimensions, component masses, centre of gravity, motor mount, `.ork` file | Rarely, as a new version | Team, measured |
| 3 | Variant options and ranges | Motors to try, ballast mass and position, parachute diameter, deploy delay | Defaults with the rocket; narrowed per run | Team |
| 4 | Run settings | Time budget, Monte Carlo runs, random seed | Per run, usually defaults | User |
| — | Generated records | Results, logged flights, calibration factors | Created by the app | Worker and flight logs |

### Recommendations

**Add competition rules (category 0).** Rules change once a year, not per run or per
rocket. Without their own category they get hard-coded or mixed into the variants. They
live in one document per season (`rules/2027`), and nothing else repeats their values.

**Keep "allowed" separate from "to try".** Rules say what is legal; variants say what to
try, and must stay inside the rules. For example, the rules approve twelve motors, but the
motor mount only fits four, so category 3 lists those four. A job with a variant outside
the rules is rejected before it runs. Limits are hard filters, never traded off inside the
score.

**Version the "fixed" components.** Rockets get repaired, glue adds mass, fins get
replaced. Each change is saved as a new rocket version and old versions are never edited.
A job refers to one exact version, its result records it, and each logged flight records
which version flew, which calibration depends on.

**Split launch conditions into weather and target.** Weather is uncertain: it is forecast
or measured and varies during a launch, so it gets a spread in the Monte Carlo runs. The
target is exact and assigned by the rules, and goes into the score. The AI parser may
suggest a target, but it is always shown to the user to confirm, never silently assumed.

**Give every number an uncertainty.** The optimizer ranks by expected score under
variation, so each input states how much it varies, stored next to the value: wind 8 mph
gusting to 15, build mass ±3 g, centre of gravity ±5 mm, motor impulse ±4%.

**Store default variant ranges with the rocket; let a run only narrow them.** What can be
tried depends on the physical rocket: parachutes the team owns, motors that fit the mount,
where ballast can go. Each rocket version carries default ranges. A run may narrow them
("only the 24 in chute today") but never widen them past the rocket's defaults or the rules.

**Only optimize what can be changed at the field.** Every variant is something a student
can swap before a flight: motor, ballast mass and position (position changes stability),
parachute diameter, deploy delay. The list stays short, so the optimizer has fewer
simulator quirks to exploit.

**Separate run settings (category 4).** Budget, Monte Carlo runs and seed control how hard
the app searches, not what it searches, so they are kept apart from conditions and
variants. With the seed recorded, any study can be replayed exactly.

### Where each category is stored

| Firestore path | Category | Notes |
|---|---|---|
| `rules/{season}` | 0 | One document per season, e.g. `rules/2027` |
| `rockets/{rocket_id}/versions/{version}` | 2 and default 3 | Never edited after creation; a change makes a new version |
| `jobs/{job_id}` | 1, 3 narrowing, 4 | Plus references to the rules and rocket version |
| `results/{job_id}` | Generated | Records the rules, rocket version and calibration used |
| `flights/{flight_id}` | Generated | Real flight data, rocket version and motor flown |
| `calibration/{calibration_id}` | Generated | Correction factors and the flights they came from |

A job stores **references** to the rules and the rocket version, and only its own
conditions, variant narrowing and run settings. Nothing is copied that could drift out of
sync, and any old job can be replayed exactly.

## Data contracts

Every step reads and writes the same JSON documents, so the simulation script, optimizer,
worker and web app can be built and tested separately. Every document carries a
`schema_version`, puts the unit in every field name, and is validated on both sides. The
values below are illustrative; motor IDs and limits are placeholders the team sets from
the rules.

Competition rules (category 0), written by the team once per season:

```json
{
  "schema_version": 2,
  "season": 2027,
  "score": { "formula": "abs_altitude_error_ft + 4 * duration_error_s" },
  "targets": {
    "qualification_apogee_ft": 800,
    "finals_apogee_ft_range": [775, 825],
    "duration_s_window": [37, 40]
  },
  "approved_motors": ["<approved motor ids>"],
  "limits": { "min_stability_cal": 1.0, "max_liftoff_mass_g": "<from rules>" }
}
```

Rocket version (categories 2 and 3), written once per build change:

```json
{
  "schema_version": 2,
  "rocket_id": "rocket",
  "version": 3,
  "created_at": "2026-11-27T18:00:00Z",
  "ork_file": "rocket_v3.ork",
  "fixed": {
    "body_diameter_in": 2.6,
    "length_in": 34.5,
    "dry_mass_g": { "value": 412, "plus_minus": 3 },
    "cg_from_nose_in": { "value": 21.2, "plus_minus": 0.2 },
    "motor_mount_mm": 24
  },
  "variant_defaults": {
    "motors": ["<ids that fit this mount>"],
    "ballast_g": [0, 40],
    "ballast_position": ["nose", "payload_bay"],
    "chute_diameter_in": [18, 24, 30],
    "deploy_delay_s": [3, 8]
  }
}
```

Job request (categories 1, 3 and 4), written by the web app:

```json
{
  "schema_version": 2,
  "job_id": "abc123",
  "rules_id": "2027",
  "rocket": { "rocket_id": "rocket", "version": 3 },
  "conditions": {
    "weather": {
      "temperature_f": 62,
      "wind_mph": 8,
      "gust_mph": 15,
      "elevation_ft": 1200,
      "pressure_inhg": 29.92,
      "launch_angle_deg": 0
    },
    "target": {
      "apogee_ft": 815,
      "duration_s_window": [37, 40]
    }
  },
  "variants": {
    "chute_diameter_in": [24]
  },
  "run": { "max_seconds": 180, "monte_carlo_runs": 100, "seed": 42 }
}
```

`variants` is optional and may only narrow the rocket version's `variant_defaults`. A
range not listed uses the default.

Job result, written by the worker:

```json
{
  "schema_version": 2,
  "job_id": "abc123",
  "status": "done",
  "rules_id": "2027",
  "rocket": { "rocket_id": "rocket", "version": 3 },
  "sim_version": "openrocket-24.12",
  "calibration_id": "cal_004",
  "designs": [
    {
      "rank": 1,
      "params": {
        "motor": "<id>",
        "ballast_g": 14,
        "ballast_position": "nose",
        "chute_diameter_in": 24,
        "deploy_delay_s": 4
      },
      "predicted": {
        "apogee_ft": { "mean": 812, "p10": 794, "p90": 830 },
        "duration_s": { "mean": 38.6, "p10": 37.4, "p90": 39.8 },
        "stability_cal": 1.8
      },
      "score": { "mean": 21, "p90": 45 }
    }
  ],
  "warnings": ["uncalibrated: fewer than 3 logged flights"]
}
```

The AI parser may write only the `conditions` object. The rules, rocket, variants and run
settings come from stored settings or the form.

## Simulation engine and calibration

The worker simulates with OpenRocket, driven headless from Python. RocketSim stays as a
cross-check on the top designs, because no evidence was found that it has a scripting or
batch interface.

| Option | Strengths | Weaknesses | Decision |
|---|---|---|---|
| OpenRocket via Python | Free, widely used, scriptable. `orlab` loads `.ork` files and runs simulations from Python, tested against OpenRocket 24.12 and 23.09 | Needs a Java runtime on the worker | Primary engine |
| RocketSim (current tool) | The team already trusts and knows it | No batch interface found | Cross-check only |
| Custom simulator in JS or Python | Full control, can run in the browser | Less accurate until calibrated; more code to maintain | Not in version 1 |

Calibration closes the gap between simulation and real flights. After each logged flight
the app fits a small set of correction factors: a drag multiplier, a parachute drag
coefficient and a motor impulse scale. Each result records the `calibration_id` it used.
A calibration only uses flights of the same rocket version, or versions the team marks as
equivalent.

Validation gate: until the simulator matches at least 3 logged flights within the team's
tolerance, every result carries an "uncalibrated" warning and the UI shows it at the top.

Time one OpenRocket run on your actual rocket before setting the default simulation
budget. It depends on the model's complexity, so any estimate of studies per minute is a
guess until measured.

## Optimizer and scoring

The optimizer minimizes expected score under uncertainty, not the nominal score. A rocket
that is perfect in calm air but misses by 60 ft in a gust loses to one that is slightly
off everywhere.

The score follows the formula from past years, to be confirmed against the 2027 rules:
altitude error in feet plus 4 times duration error in seconds, where duration error is
the distance outside the 37 to 40 s window. The formula is read from the season's rules
document, not hard-coded.

```
S = |h − h_target| + 4·d,    d = max(0, 37 − t, t − 40)
```

Search runs in three stages over the variants (category 3):

1. Coarse sweep. Grid or random search over motor (discrete), ballast mass and position,
   parachute size and deployment delay. This also shows the shape of the problem.
2. Refinement. Bayesian optimization or CMA-ES around the best regions, once the simulator
   is trusted.
3. Robustness check. Monte Carlo on the top 10 designs, around 100 runs each, varying
   wind, gusts, temperature, launch angle, motor impulse and build mass by the
   uncertainties stored with each value.

Ranking uses the mean score, with the 90th percentile as a tiebreaker and a shown risk
number. Hard filters from the rules (motor list, stability margin, size and mass limits)
are applied before scoring, never traded off inside it.

Every study takes a random seed and a time budget. The same inputs and seed give the same
ranking, and a study stops when its budget runs out and returns the best designs found so far.

## AI input mapping

Claude turns free text such as "windy, about 55F, finals target 810, field is at 1,000 ft"
into the `conditions` object (category 1), and the user confirms it before anything runs.
The model fills in a form. It never runs code, picks rocket parameters or variants, or
starts a job.

1. The user types text. The parse function sends it to Claude with the conditions schema
   and the current form values, and forces a single tool call that returns the
   `conditions` JSON.
2. Code validates the result: types, units, allowed ranges (for example wind 0 to 40 mph),
   and drops unknown fields. Unit conversion is done in code where possible.
3. If a required field is missing or ambiguous, the model returns a short question instead
   of a guess, and the UI asks it.
4. The UI shows an editable form pre-filled with the parsed values. Values the user stated
   and values the model inferred are marked differently. A target is always shown for
   confirmation.
5. The user confirms. Only then is the job written to Firestore.

| Failure | Mitigation |
|---|---|
| Wrong number or unit (mph read as km/h, °C as °F) | Code-side range checks, an editable confirm step, and inferred values flagged |
| Missing field filled with an invented default | Required fields come back as questions, never guesses |
| Instructions hidden in the text (prompt injection) | The model's output is only data in a fixed schema. It cannot add fields, run code or change rules, rocket or variants |
| API outage or cost spike | The manual form always works. Per-user rate limit and a budget alert on the API key |
| Silent drift in quality | A fixed set of about 30 example phrases with expected JSON, re-run after any prompt or model change |

The raw text and the parsed JSON are both stored with the job, so mistakes can be traced.
The API key lives only in the parse function, never in the browser. Where that function
runs is undecided: a Cloud Function (needs Firebase's paid plan) or a `parse` job handled
by the worker (no extra service, but slower and tied to the worker being up).

## Web app, worker, and deployment

| Part | Choice | Why |
|---|---|---|
| Web app | Static site on GitHub Pages | Free, simple, no server to run |
| Sign-in | Firebase Auth with Google, allowlisted accounts | No passwords to manage; blocks strangers from running jobs |
| Data | Firestore (project `athens-2509f`): `rules`, `rockets` (with `versions`), `jobs`, `results`, `flights`, `calibration` | Live listeners let the page update when results arrive |
| Worker | Python process on a rented VM or a laptop, polling Firestore for pending jobs | No inbound port, no HTTPS or CORS setup, and the same code runs on a laptop during development |
| Simulator | OpenRocket (Java) plus the optimizer, packaged in one container | Reproducible install on any machine |
| Uploads | Altimeter CSVs parsed in the browser and stored as numbers | Avoids needing file storage for version 1 |
| Weather | A free forecast API called from the browser to pre-fill conditions | Editable by the user, since forecasts are often wrong |

A job moves through four states: `pending` (written by the web app), `running` (claimed by
the worker), `done` or `failed` (written by the worker, with the result or an error
message). The worker claims a job in a transaction so two workers never take the same one,
and it marks jobs that have run too long as `failed`.

Cost: GitHub Pages and the Firebase free tier cover a small team. Cloud Functions and some
Firebase storage features may need Firebase's pay-as-you-go plan, so verify current plan
requirements before relying on them. The VM is the main recurring cost, and it is optional
until the team wants studies to run without a laptop.

Field use: launch fields often have weak signal. Before launch day the worker precomputes
studies across a grid of conditions (temperature, wind, elevation, target) and saves them
as one-page design cards the team can look up offline.

## Security and cost controls

- Firestore security rules: signed-in, allowlisted users may create jobs and read their
  team's data. Only the worker may write results and calibration. Rules and rocket versions
  may be created by team members but never edited after creation.
- The worker's Firebase service-account key and the Claude API key never go in the GitHub
  repo or the browser. Firebase web config values in the site code are public by design;
  the rules are what protect the data.
- Per-user limit on queued jobs and on parse requests, plus budget alerts on Firebase, the
  VM and the Claude API.
- The worker runs only tested code. It never executes code written by the AI.
- Uploads are size-limited and parsed with a strict CSV parser. Parsed numbers are
  range-checked before they can affect calibration.
- The VM runs the worker as a non-root user, has no open inbound ports, and gets automatic
  security updates.
- Every study stores its inputs, seed, simulator version, rules ID, rocket version and
  calibration ID so results can be reproduced.

## Limitations and risks

The biggest limit is simulator accuracy: if the sim is off by 40 ft, a perfect optimizer
returns a confidently wrong answer. Everything else is secondary to that.

| Risk | Effect | Mitigation |
|---|---|---|
| Simulator disagrees with real flights | Recommendations miss the target | Validation gate, calibration from logged flights, "uncalibrated" warning |
| Few logged flights early in the season | Calibration is noisy | Show the number of flights behind each calibration; widen uncertainty when it is small |
| Forecast differs from conditions at launch | Design tuned for the wrong weather | Treat the forecast as the center of a range; re-run with measured conditions at the field |
| Optimizer exploits quirks of the simulator | Designs that only work in the sim | Few variants, sensible ranges, robustness check, and a rule that the top picks must be flown before trusting them |
| Motor-to-motor variation | Real impulse differs from the thrust curve | Vary impulse in Monte Carlo; log the motor used with every flight |
| Rocket changes after results were computed | Results and calibration no longer match the rocket | Rocket versions are never edited; jobs, results and flights record the version |
| AI misreads the user's text | Wrong conditions in a job | Confirm step, range checks, flagged inferences |
| Worker offline | Jobs stay `pending` | Show queue age in the UI; precomputed design cards as backup |
| Rules change each year | Constraints and score formula go stale | One rules document per season; re-check it against each year's rules PDF |
| Firebase or API pricing and plan changes | Unexpected cost or lost features | Verify current terms before launch; budget alerts; keep the manual path working |

The app does not certify safety or legality. Stability, motor and construction rules are
the team's and the range safety officer's responsibility.

## Build plan and open questions

Each step produces something that works alone, so the next step can be tested against it.

1. Validate. Reproduce one or two real flights in the simulator. Fix the model until it
   agrees with the team's tolerance.
2. Step 1: write the JSON schemas for rules, rocket version, job request and job result,
   and a script that runs one simulation from a request JSON and returns a result JSON.
3. Step 2: a script that takes launch conditions, runs the sweep, Monte Carlo and ranking,
   and returns designs.
4. Step 3: the worker that polls Firestore, runs step 2 and writes results; set up the VM
   and security rules.
5. Step 4: the static web app with sign-in, the rocket and conditions forms, live job
   status and results.
6. Add flight logging and calibration, then weather pre-fill.
7. Add the AI parser last, once the form and the schemas are stable.

Done so far: home page on GitHub Pages; Firestore project `athens-2509f` created, with a
read/write connection test passing and security rules verified.

Open questions for the team:

- [ ] What simulation tolerance counts as "validated" (apogee and duration)?
- [ ] Which approved motors and rocket constraints apply, per the 2027 rules and approved motor list?
- [ ] Is the 2027 score formula still altitude error plus 4 times duration error?
- [ ] Does the parse function run as a Cloud Function or as a worker job?
- [ ] Who owns the VM, the Firebase project and the API key, and who sets the budget alerts?
- [ ] What is the team's first real flight to reproduce, and what data did the altimeter record?
- [ ] Which variants can really be changed at the field (ballast positions, parachutes on hand, delay settings)?
- [ ] What uncertainty should each measured value carry (mass, centre of gravity, motor impulse)?
- [ ] When the rocket changes, which changes need a new calibration and which can reuse the old one?
