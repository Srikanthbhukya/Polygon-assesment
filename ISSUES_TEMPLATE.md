# Issues Analysis

> AI disclosure: this analysis was drafted with AI assistance (Claude) from the codebase, then reviewed by me. Each item says whether it was **Observed** (seen in the running app) or **Code-read** (concluded from reading the code). Edge-case answers below are code-read unless stated otherwise.

## Summary

| Type | Critical | High | Medium | Low | Total |
|------|----------|------|--------|-----|-------|
| Product Issues | 0 | 2 | 1 | 0 | 3 |
| Code Issues | 0 | 3 | 0 | 0 | 3 |

---

## Product Issues

### [P1] Every button re-runs the whole Polygon fetch, so each action takes minutes

**Severity**: High

**Location**: `problems/views.py::index()`, `problems/polygon_api.py::download_and_extract_package`, `get_all_test_cases`; `problems/templates/problems/index.html` (loading text)

**Description**:
Any POST with a `problem_id` makes `index()` fetch everything again: `problem.info`, the package (`problem.packages` + `problem.package`), `problem.checker`, `problem.tests`, and, when nothing is cached, `problem.testInput` and `problem.testAnswer` for every test (1 + 2N calls). This happens again for "Create/Update problem in Database", "Migrate Test Description to Database" and "Migrate Test Cases to Cloud Storage". Some calls repeat inside one request (`problem.checker` is called twice, `problem.info` is called again inside `download_and_extract_package`). The page warns "Fetching problem data and test cases (may take 3-4 minutes)...". *(Code-read; the 3-4 minute message is in the template.)*

**Impact**:
- A user who wants to run the three migrations waits through three long, blocking requests.
- No progress is shown, so users may refresh or double-click, and a gateway timeout can leave them unsure whether anything was saved.
- Polygon failures and rate limits hit the user directly.

**Suggested Fix**:
Separate "fetch/preview" from "migrate". Fetch once and reuse that snapshot (the Redis snapshot already exists) for all later actions, so a migration button only does its own work. Run long operations as background jobs with a visible status (queued, running, done, failed) and disable the button while running.

**Priority rationale**:
It is the first thing every user hits on every use, and it makes every other problem more expensive (a failed 3-minute request is costly to retry). It is above P2 and P3 because the other issues only matter once a user can get through the flow at a reasonable speed.

---

### [P2] Re-migrating silently overwrites data, and the review step is unreliable

**Severity**: High

**Location**: `problems/views.py::index()` (`constraints = ''`, `editorial = ''`, `problem_obj.constraints = constraints`, tag handling); `problems/templates/problems/index.html` (`|linebreaksbr` on statement, input/output format, notes)

**Description**:
- `constraints` and `editorial` are hard-coded to empty strings in `index()` and written back on every update, so any value an admin entered for those fields is wiped when the problem is re-migrated.
- The statement, input format, output format and notes are HTML fragments shown through `{{ ...|linebreaksbr }}`, which escapes tags, so the user probably sees raw HTML text rather than the rendered problem. *(Code-read; confirm in the UI.)*
- Tags are free text with `get_or_create`, so a typo creates a new `ProblemTag` with no warning.

**Impact**:
- Curated content is lost on a routine "update", with no warning or diff.
- Users can't check what learners will actually see before saving.
- The tag list fills up with near-duplicates, which hurts filtering later.

**Suggested Fix**:
Only update fields that Polygon actually provides and leave `constraints` and `editorial` untouched. Show a "what will change" summary before an update and ask for confirmation. Render the statement in a sandboxed preview. Suggest existing tags and normalize new ones.

**Priority rationale**:
It causes real data loss and wrong content reaching learners, which is worse than a confusing flow (P3). It ranks below P1 because it only affects problems that were edited by hand or re-migrated, while P1 affects every run.

---

### [P3] The workflow and its state are unclear: users can't tell what has been migrated

**Severity**: Medium

**Location**: `problems/templates/problems/index.html` (button labels, disabled states); `problems/views.py::index()` (`migrate_to_azure` flag, 260-character truncation)

**Description**:
Migration is three separate buttons with a hidden order (problem to DB, then test cases to DB, then cloud storage); the later two are just disabled until the problem exists. "Migrate Test Description to Database" actually stores each test's input and output cut to 260 characters, while the real files go to cloud storage. The cloud button's field is still named `migrate_to_azure` although the provider is configurable. After an action the user gets one message; the page never shows what is currently in the DB or storage, or whether they are in sync with Polygon.

**Impact**:
- A new user can skip or repeat steps, or assume full test cases are in the database when only truncated copies are.
- After changing a problem on Polygon, there is no way to see whether the DB or storage is stale.

**Suggested Fix**:
Show one guided flow with a status panel (problem in DB and last updated, test cases in DB and count, files in storage and count, checker present). Name buttons for what they do ("Save test-case previews to DB", "Upload test files to storage") and state the 260-character limit on the page.

**Priority rationale**:
Users can still succeed once they learn the order, and nothing is lost, so it ranks last. It is still in the top three because it affects every migration and is the main reason the tool needs a developer's help to use safely.

---

<!-- Three product issues, ranked from highest to lowest priority. -->

---

## Code Issues

### [C1] One very large view does everything, with duplicated logic

**Severity**: High

**Location**: `problems/views.py::index()`; `problems/polygon_api.py` (Redis methods); `problems/urls.py`

**Description**:
`index()` is a single function of several hundred lines that handles four different actions through flag fields (`migrate_to_db`, `migrate_test_cases_to_db`, `migrate_to_azure`) in one POST branch, mixing request parsing, Polygon calls, database writes, cloud storage and template context. Logic is copied in several places: the "get test cases from Redis, fall back to Polygon, re-cache" block appears several times (and again in `migrate_to_cloud_storage`), the main-solution fetch appears twice, and `PolygonAPI` repeats the Redis connection setup in four methods (`delete_problem_test_case_cache`, `store_test_cases_in_redis`, `get_test_cases_from_redis`, `clear_test_cases_from_redis`). `problems/urls.py` has one route for everything. The existing tests cover the storage layer and `migrate_to_cloud_storage`, but not the view.

**Impact**:
Any change (a new storage provider, a new field, a new action) means editing a function that is hard to read and test, with a high risk of breaking another branch. Bugs are fixed in one copy and left in others.

**Suggested Fix**:
Split into one view/endpoint per action, move the logic into service functions (fetch problem, save problem, save test cases, upload test cases), and add one shared Redis client and one "get test cases" helper. Keep views thin and add tests for the services.

**Priority rationale**:
It is the root cause of most other code problems and the biggest barrier to change, so it is first. Fixing it also makes C2 and C3 much easier to address.

---

### [C2] Database, cloud storage and Redis are handled inconsistently, and storage updates are destructive

**Severity**: High

**Location**: `problems/views.py::index()` (`transaction.atomic()`, `except` block); `problems/polygon_api.py::migrate_to_cloud_storage`, `delete_problem_files`

**Description**:
`transaction.atomic()` covers only the database. Cloud storage and Redis are changed inside the same request and "undone" by hand in the `except` block (`delete_problem_files`, `clear_test_cases_from_redis`). In `migrate_to_cloud_storage`, `storage.empty_blob(...)` deletes all existing files for the problem before uploading new ones, so a failure halfway leaves partial or no test files. The upload also happens before the rest of the request (fetch, parse, DB steps), so a later failure triggers cleanup that deletes files which had uploaded correctly. `delete_problem_files` catches and only logs its own errors, which can leave orphaned files.

**Impact**:
A failed or interrupted migration can leave a live problem without valid test files, so submissions on the platform could fail or be judged wrongly. DB, storage and cache can drift apart with no record of the real state.

**Suggested Fix**:
Run the cloud upload as its own step after the DB commit. Upload to a new versioned prefix (or temporary keys), switch over only when every file succeeds, then remove the old files. Store the result (status, file count, timestamp) in the database so the state is visible and retries are safe.

**Priority rationale**:
Less visible than C1, but it carries the highest production risk because it can break problems that are already live. It is second because C1 is why this logic is hard to fix cleanly.

---

### [C3] Test-case syncing is positional, and the data model doesn't track sync state

**Severity**: High

**Location**: `problems/views.py::index()` (sample and test-case update loops, `test_case_count`, `slug`); `problems/models.py` (`Problem.slug`, `ProblemTestCase`, `SampleTestCase`)

**Description**:
`SampleTestCase` and `ProblemTestCase` rows are updated by list position (`if idx < len(existing_...)`), not by a stable key such as the Polygon test index, and extra old rows are never deleted. `idx` and `order` differ when a test is skipped for empty input or output, and the cloud upload uses its own numbering (`enumerate(..., start=1)`), so DB order and storage file numbers can disagree. `Problem.test_case_count` is set from `len(api.get_test_cases(...))`, which counts every Polygon test, including skipped ones. `slug = slugify(title)` is `unique=True` and is overwritten on update.

**Impact**:
After a problem changes on Polygon, the DB can hold stale or mismatched tests, and the stored count may not match what exists. Duplicate titles break migration, and a changed title changes the problem's URL.

**Suggested Fix**:
Key test cases on `(problem, polygon_test_index)`, delete rows no longer present, use one numbering scheme for both DB and storage, compute `test_case_count` from what was actually saved, and generate slugs with collision handling without changing an existing slug.

**Priority rationale**:
It produces quiet data errors that surface later and are hard to trace, but it is narrower than C1 (all changes) and C2 (live-problem breakage), so it is third.

---

<!-- Three code issues, ranked from highest to lowest priority. -->

---

## Edge Case Analysis

*(All answers below are from reading the code; I have not tested them unless stated.)*

### Q1

> A Polygon problem has 0 sample test cases but 15 regular test cases. What happens when you migrate this problem?

**Action discussed:** all three migrations in order: "Create/Update problem in Database", "Migrate Test Description to Database", "Migrate Test Cases to Cloud Storage".
**Basis:** code-read, not tested.

The migration succeeds with no error. In "Create/Update problem in Database", `sample_tests` is an empty list, so no `SampleTestCase` rows are created (and any existing sample rows from an earlier migration are left in place, not removed). "Migrate Test Description to Database" saves all 15 tests as `ProblemTestCase` rows with `is_sample=False`, and the cloud upload writes 15 input/output pairs (`01` to `15` and `.a`). `test_case_count` is 15. The result is a valid problem with no examples to show learners on its page, and nothing warns the user about that.

### Q2

> A problem is migrated with 20 test cases. Later, the problem setter removes 8 test cases on Polygon (now 12 remain). The problem is re-migrated. What happens?

**Action discussed:** re-running "Create/Update problem in Database", "Migrate Test Description to Database", and "Migrate Test Cases to Cloud Storage".
**Basis:** code-read, not tested.

- `Problem.test_case_count` is updated to 12.
- `ProblemTestCase`: the first 12 existing rows are overwritten by position; rows 13 to 20 are never deleted, so the DB still holds 20 rows, 8 of them stale.
- Cloud storage: `empty_blob` deletes the old files and 12 new pairs are uploaded, so storage is correct (`01` to `12`).
- Result: storage and the `test_case_count` say 12, but the test-case table still has 20 rows. If the Redis cache from an earlier fetch (30-minute TTL) is still present, the migration buttons use it instead of Polygon, so re-fetching first matters.

### Q3

> Two different Polygon problems have the exact same title: "Two Sum". You migrate the first one successfully. Then you try to migrate the second one. What happens?

**Action discussed:** "Create/Update problem in Database" for the second problem.
**Basis:** code-read, not tested.

The second problem has a different `polygon_id`, so `index()` creates a new `Problem` with `slug = slugify("Two Sum")`, the same as the first. `Problem.slug` is `unique=True`, so the insert raises an integrity error inside `transaction.atomic()`. The `except` block catches it, the transaction is rolled back, and the user sees "Migration failed and all changes have been rolled back. Reason: ..." followed by the raw database error text. Nothing is saved. The user has no way to fix this in the tool and would have to rename the problem on Polygon. A different capitalization such as "two sum" produces the same slug and fails the same way.

### Q4

> When test cases are saved to the database via "Migrate Test Cases to DB", some data is intentionally discarded. What data is lost? Why might this cause problems?

**Action discussed:** "Migrate Test Description to Database" (the button the task calls "Migrate Test Cases to DB"; code: `migrate_test_cases_to_db` branch of `index()`).
**Basis:** code-read, not tested.

- **Truncation:** only the first 260 characters of each input and output are stored in `ProblemTestCase`.
- **Trailing whitespace:** `.rstrip()` is applied to input and output (also for sample test cases), removing trailing newlines and spaces.
- **Skipped tests:** a test with an empty input or output is skipped entirely, so it gets no row.
- **Polygon metadata:** the Polygon test index and the `manual` flag are not stored; order is assigned by position.

Why it matters: the DB copy of large tests is cut mid-line, so it can't be used to re-run or verify a solution, and it differs from the full copy in cloud storage (which is not truncated or stripped). Stripping whitespace can matter for exact-match checkers. Skipping tests with empty output silently drops valid cases. Anyone who later treats the database as the source of truth would get incomplete data.

---

## Notes

- Unused imports in `problems/views.py` (`BeautifulSoup`, `settings`, `cache`); a linter (ruff/flake8) would catch these.
- `PolygonAPI._make_request` has an `expect_json` branch that can never return text in its `except` path (dead code).
- `users/views.py::login_view` logs out an already-authenticated visitor who opens the login page.
- Raw exception text is shown to the user in the error box, which can expose database details.
- The server compiles checker source code fetched from Polygon with `g++`; it is only compiled, not run, but the build environment should be isolated.
- The cloud field name `migrate_to_azure` and the docstrings still refer to Azure, which is misleading now that the provider is configurable.