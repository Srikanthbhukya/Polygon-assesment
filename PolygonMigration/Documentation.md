# Functionality Documentation: Polygon Problem Migrator ("Hatsu")

A Django tool that lets a staff user pull a problem from [Polygon](https://polygon.codeforces.com), store the problem in the database, and upload its test cases to cloud storage. This deployment uses **Supabase Storage**, accessed through its S3-compatible API (`STORAGE_PROVIDER=s3`). Azure Blob Storage is supported by the code as an alternative provider but is not used.

## 1. Architecture at a glance

| Layer | File | Role |
|---|---|---|
| Template (browser) | `problems/templates/problems/index.html` | One page with several small forms and a tag picker (vanilla JS, Bootstrap) |
| View | `problems/views.py` → `index()` | The only endpoint. Every button on the page POSTs here; a hidden field decides what runs |
| Polygon client | `problems/polygon_api.py` → `PolygonAPI` | Signs and sends Polygon requests, downloads the package, caches test cases in Redis, drives the cloud upload |
| Storage layer | `problems/storage.py` (`get_storage`, `get_container_name`, `get_provider_name`), `S3Testcase.py` (`S3StorageManager`), `AzureTestcase.py` | Provider-independent upload interface. `STORAGE_PROVIDER` selects S3 or Azure |
| Models | `problems/models.py` | `Problem`, `SampleTestCase`, `ProblemTestCase`, `ProblemTag` |
| URL | `problems/urls.py` | `app_name = 'problems'`, `path('', views.index, name='index')` |
| Auth | `users/views.py`, `users/backends.py`, `users/models.py`, `users/urls.py` | Email + password login, staff-only access, custom `User` model |
| Tests | `problems/tests.py` | S3 layout, provider switch, and migration-flow tests |

**Single endpoint, multiple actions.** `index()` is the only view. The form that was submitted is identified by which hidden field is present in `request.POST`:

| Hidden field | Button on the page | Action |
|---|---|---|
| *(none, only `problem_id`)* | Fetch Problem | Fetch and display only |
| `migrate_to_db=1` | Create/Update problem in Database | Save problem, tags, sample tests |
| `migrate_test_cases_to_db=1` | Migrate Test Description to Database | Save all test cases (truncated) |
| `migrate_to_azure=1` | Migrate Test Cases to Cloud Storage | Upload to the configured provider (the field name is historical; it is not Azure-only) |

**External services:** Polygon API, Redis (test-case cache), Supabase Storage (via the S3-compatible endpoint, `S3_ENDPOINT_URL` of the form `https://<project>.supabase.co/storage/v1/s3`), the relational database (Django ORM). Azure Blob Storage is an optional alternative (`STORAGE_PROVIDER=azure`).

**Configuration (Django settings / `.env`):** `POLYGON_API_KEY`, `POLYGON_API_SECRET`, `STORAGE_PROVIDER` (`s3` or `azure`), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `S3_ENDPOINT_URL`, `AWS_S3_BUCKET_NAME`, `AZURE_CONTAINER_NAME`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_SSL`, `CUSTOM_CHECKER_DIR`.

**How the code is organized in `index()`:** after the access check, each POST runs inside one `transaction.atomic()` block, so a failure rolls back every database change from that request. Cloud uploads and Redis are not transactional, so the `except` block undoes them by hand (see section 7).

---

## 2. Flow 1: User login

**Code:** `users/urls.py` (routes), `users/views.py::login_view` and `logout_view`, `users/backends.py::EmailBackend`, `users/models.py::User`, `users/templates/users/login.html`; the access gate on the migrator page is the decorator on `index()` in `problems/views.py`:

```python
@user_passes_test(lambda u: u.is_authenticated and u.is_staff, login_url='/users/login/')
def index(request): ...
```

Routes (`users/urls.py`): `/users/login/` (name `login`) and `/users/logout/` (name `logout`).

1. **What the user clicks or submits.** An anonymous (or non-staff) user who opens the migrator page is redirected to `/users/login/`. They enter their email and password on the login form and submit it. If they are already logged in, the navbar on the migrator page shows "Welcome, {first_name} {last_name}" and a **Logout** button.
2. **What the frontend sends.** Login: `POST /users/login/` with `email` and `password` (the two fields `login_view` reads from `request.POST`). Logout: the navbar form sends `POST /users/logout/` with the CSRF token.
3. **What the backend does, in order.**
   1. `login_view` first checks `request.user.is_authenticated`; if the visitor already has a session, it calls `logout(request)` so the login page always starts clean.
   2. On `POST`, it reads `email` and `password` and calls `authenticate(request, username=email, email=email, password=password)`.
   3. `authenticate` runs `EmailBackend.authenticate` (a `ModelBackend` subclass in `users/backends.py`). It takes the email from `kwargs['email']` (falling back to `username`), loads the user with `User.objects.get(email=email)`, and returns the user only if `check_password(password)` is true and `user_can_authenticate(user)` is true (the user must have `is_active=True`). Otherwise it returns `None`.
   4. If `authenticate` returns a user and `user.is_staff` is true, `login(request, user)` creates the session and the view redirects to `problems:index` (the migrator page).
   5. If the user exists but is not staff, no session is created and the message "You do not have staff access." is queued. If authentication fails, the message "Invalid email or password." is queued. In both cases `users/login.html` is rendered again.
   6. Logout: `logout_view` calls `logout(request)` (destroys the session) and redirects to the URL named `login`.
   7. On every later visit to the migrator page, `user_passes_test` re-checks `is_authenticated and is_staff`; if the check fails, the request is redirected to `/users/login/` and `index()` does not run.
4. **External APIs.** None. Authentication is entirely local: no Polygon, cloud, or third-party login provider is involved.
5. **Reads and writes.**
   - DB read: the `User` table (`users.User`, a custom model with `USERNAME_FIELD = 'email'`) to look up the user by email and verify the password hash.
   - Session: `login()` writes a session (Django's session framework); `logout()` clears it.
   - Nothing is written to the problem tables or to cloud storage in this flow. Staff accounts are created with `UserManager.create_superuser` or by setting `is_staff=True` on a user.
6. **What the user sees.** On success, they land on "Hatsu: Problem Migrator" with their name in the navbar. On failure, they stay on the login page with an error message ("Invalid email or password." or "You do not have staff access."). After logout, they are returned to the login page.

---

## 3. Flow 2: Fetching a problem from Polygon

**Code:** `problems/views.py::index`, `parse_problem_html`; `problems/polygon_api.py::PolygonAPI`.

1. **What the user clicks or submits.** The user types a Polygon problem ID and clicks **Fetch Problem** (`#fetchForm`). JavaScript intercepts the submit, shows the loading overlay ("Fetching problem data and test cases (may take 3-4 minutes)..."), then submits the form.
2. **What the frontend sends.** `POST` to the same page with `csrfmiddlewaretoken` and `problem_id`. No `migrate_*` field is present.
3. **What the backend does, in order.**
   1. `index()` builds the tag list for the dropdown (`ProblemTag`, serialized as `all_tags_json`).
   2. It creates `PolygonAPI()` and looks up `Problem.objects.filter(polygon_id=...)`. If the problem is already in the database, it pre-fills `difficulty` and selected tags from the DB and fetches the main solution (see step 4).
   3. Inside `transaction.atomic()`: `api.get_problem_info()` gets name, time limit, memory limit.
   4. `api.download_and_extract_package()` lists packages, picks the highest-revision `standard` package, downloads the ZIP, extracts it in a temp directory, reads `problem.html`, and deletes the temp directory.
   5. `parse_problem_html()` (lxml) extracts title, legend (statement), input format, output format, and notes.
   6. Test cases: with no migrate flag, `api.get_all_test_cases()` is called and the result is cached with `store_test_cases_in_redis(..., expiry_hours=0.5)`.
   7. It computes the slug (`slugify(title)`), reads the checker (`problem.checker`) and normalizes it (`std::ncmp.cpp` becomes `ncmp`, unknown names become `custom`), checks for a custom checker (`get_custom_checker_info`), and counts tests (`get_test_cases`).
   8. All this is placed in `context['fetched_problem']` and `context['all_test_cases']` (previews truncated to 50 chars) for the template. Nothing is written to the database in this flow.
4. **External APIs called (Polygon, `POST https://polygon.codeforces.com/api/<method>`).**
   `problem.info`, `problem.packages`, `problem.package`, `problem.tests`, `problem.testInput` and `problem.testAnswer` (once per test), `problem.checker`. If the problem is already in the DB: `problem.updateWorkingCopy`, `problem.solutions`, `problem.viewSolution`.
   Each request is signed in `PolygonAPI._generate_api_sig`: parameters plus `apiKey` and `time` are sorted, a 6-character random prefix is added, and `apiSig = prefix + sha512(prefix/method?params#secret)`.
5. **Reads and writes.**
   - DB: reads `Problem`, `ProblemTag`.
   - Redis: writes `polygon_migration_test_cases_<polygon_id>_count` and `..._test_<n>` (JSON per test: `index`, `input`, `output`, `description`, `is_sample`), 30-minute TTL.
   - No cloud storage writes.
6. **What the user sees.** A "Fetched Problem" section: slug, difficulty selector, tag picker, time/memory limit, checker type, test count, the statement/input/output/notes HTML, and a table of all test cases with truncated previews and a "View Full" modal. If the problem is not in the DB yet, the DB ID and Algopath link say "Please migrate to database first" and the cloud/test-case buttons are disabled. On a Polygon error, a red error box shows the message.

---

## 4. Flow 3: Migrating a problem to the database

**Code:** `index()` in `problems/views.py` (branch `if migrate_to_db:`), models in `problems/models.py`.

1. **What the user clicks or submits.** After fetching, the user selects a **Difficulty** (required), optionally picks or adds tags, and clicks **Create/Update problem in Database** (`#migrate-db-form`). The form's JS blocks submit with an alert if no difficulty is selected, copies the selected tags into hidden inputs (`updateHiddenInputs`), and shows a spinner.
2. **What the frontend sends.** `POST` with `problem_id`, `migrate_to_db=1`, `difficulty` (`easy`/`medium`/`hard`), and one `tags` value per selected tag.
3. **What the backend does, in order.**
   1. Re-runs the fetch steps from flow 2 (info, package, parse HTML, checker, test count). Test cases come from Redis if cached, otherwise from Polygon.
   2. Validates that `difficulty` is present. If not, it raises and the transaction is rolled back.
   3. `Problem.objects.filter(polygon_id=...)`: if found, it updates title, slug, difficulty, statement, input/output format, time and memory limit, checker type, test count, notes, and saves. If not found, `Problem.objects.create(...)`.
   4. Tags: `extra_tags.clear()`, then for each submitted tag `ProblemTag.objects.get_or_create(tag_name=...)` and `extra_tags.add(tag)`. New tags typed in the picker are created here.
   5. Sample tests: filters test cases with `is_sample=True`; for each with non-empty input and output, it updates the existing `SampleTestCase` at that position or creates a new one, with `order` starting at 1.
   6. Re-reads the problem from the DB and fetches the main solution (tag `MA`, or the first solution) for display.
4. **External APIs.** Same Polygon calls as flow 2, plus `problem.solutions` and `problem.viewSolution`. No cloud calls.
5. **Reads and writes.**
   - DB writes: `Problem` (create/update), `ProblemTag` (get_or_create), `Problem.extra_tags` (M2M), `SampleTestCase` (create/update).
   - Redis: reads cached test cases (or refills the cache on a miss).
6. **What the user sees.** A blue "Database Operation" box: "Problem '<title>' created/updated in database successfully." (plus a custom-checker note if there is one). The page now shows the database problem ID and the link `https://www.algopath.ai/problems/<slug>`, and the main solution with a Copy button. On failure: "Migration failed and all changes have been rolled back. Reason: ...".

---

## 5. Flow 4: Migrating test cases to the database

**Code:** `index()` in `problems/views.py` (branch `if migrate_test_cases_to_db:`), models `ProblemTestCase`.

1. **What the user clicks or submits.** Under the test case table, the user clicks **Migrate Test Description to Database** (`#migrate-test-cases-form`). The button is disabled until the problem exists in the DB.
2. **What the frontend sends.** `POST` with `problem_id` and `migrate_test_cases_to_db=1`.
3. **What the backend does, in order.**
   1. Re-runs the fetch/display steps (flow 2).
   2. Looks up the `Problem` by `polygon_id`. If missing, it raises "Please migrate the problem to the database first."
   3. Gets the test cases from Redis (`get_test_cases_from_redis`), falling back to `get_all_test_cases` and re-caching.
   4. Loads the existing `ProblemTestCase` rows for the problem. For each Polygon test with non-empty input and output, it truncates input and output to 260 characters, then updates the row at the same position or creates a new `ProblemTestCase` (`is_sample`, `input`, `output`, `description`, `order`).
4. **External APIs.** Polygon only if the Redis cache is empty or expired (`problem.tests`, `problem.testInput`, `problem.testAnswer`).
5. **Reads and writes.**
   - DB: reads `Problem` and `ProblemTestCase`; writes `ProblemTestCase` rows.
   - Redis: read, or refill on a miss.
   - Only the first 260 characters of each test are stored in the DB; the full data goes to cloud storage (flow 5).
6. **What the user sees.** A green box: "Test cases description migrated to database Successfully." On failure, a rollback error message.

---

## 6. Flow 5: Uploading test cases to cloud storage

**Code:** `index()` (branch `if migrate_to_azure:`), `PolygonAPI.migrate_to_cloud_storage`, `upload_custom_checker`, `_compile_and_upload_checker`, `delete_problem_files`, `delete_problem_test_case_cache`; storage in `problems/storage.py`, `S3Testcase.py`, `AzureTestcase.py`.

1. **What the user clicks or submits.** The user clicks **Migrate Test Cases to Cloud Storage** (`#migrate-azure-form`; disabled until the problem is in the DB). A spinner shows "Migrating to cloud storage...".
2. **What the frontend sends.** `POST` with `problem_id` and `migrate_to_azure=1`.
3. **What the backend does, in order.**
   1. Looks up the `Problem`. If it is not in the DB, it returns an error telling the user to migrate the problem first.
   2. Reads the provider and container: `get_provider_name()` and `get_container_name()` (`STORAGE_PROVIDER`, `AWS_S3_BUCKET_NAME` or `AZURE_CONTAINER_NAME`).
   3. Uses the database ID (`problem_obj.id`) for naming, not the Polygon ID.
   4. `delete_problem_test_case_cache(db_id)` deletes the main platform's Redis keys matching `oj_dev_with_redis_storage_test_cases_<db_id>*` so no stale tests stay cached.
   5. `get_custom_checker_info()` detects a non-standard checker and sets an info message.
   6. `migrate_to_cloud_storage()`:
      - gets the test cases from Redis, or from Polygon on a miss;
      - `storage.empty_blob(container, db_id)` deletes the old files for the problem;
      - for each test with both input and output, `storage.upload_test_case(container, db_id, idx, input, output)` (incomplete tests are skipped with a warning);
      - `upload_custom_checker()` fetches the checker source (`problem.viewFile`, trying `source`, then `resource`, then with `.cpp`), compiles it with `g++ -std=gnu++17 -O2` in `CUSTOM_CHECKER_DIR` (or a temp dir), and uploads the binary. If compilation fails it uploads the source as `custom_checker.cpp` instead.
   7. Sets the success message, then the view continues with the display steps from flow 2.
4. **External APIs.** Polygon: `problem.tests`, `problem.testInput`, `problem.testAnswer` (if not cached), `problem.checker`, `problem.viewFile` (custom checker). Cloud: Supabase Storage through its S3-compatible API (`put_object`, `delete_object`, `list_objects_v2` via `S3StorageManager`). With `STORAGE_PROVIDER=azure` the same calls go to Azure Blob Storage instead.
5. **Reads and writes.**
   - Cloud storage layout (Supabase bucket named by `AWS_S3_BUCKET_NAME`, as verified in `tests.py`): `test_cases/<db_id>/<NN>` for input and `test_cases/<db_id>/<NN>.a` for output, with `NN` zero-padded to two digits; plus `test_cases/<db_id>/custom_checker` (`.exe` on Windows) or `custom_checker.cpp`. The Azure backend (`AzureTestcase.py`) implements the same interface but is not used in this deployment.
   - Redis: deletes the main platform's test-case keys; reads or refills `polygon_migration_test_cases_<polygon_id>_*`.
   - DB: read-only here.
6. **What the user sees.** A green box: "Test cases migrated to cloud storage (<provider>) successfully." (plus a note if a custom checker was compiled and uploaded). On failure, a red error with the reason.

---

## 7. Error handling and rollback (applies to flows 3 to 5)

- Each POST runs inside `transaction.atomic()`, so any exception rolls back all database changes from that request.
- Cloud storage is not transactional. If the upload itself fails, `delete_problem_files(db_id)` removes partial uploads. If a later step in the same request fails, the outer `except` removes the files uploaded earlier (`storage_uploaded` and `storage_problem_id`).
- The outer `except` also calls `clear_test_cases_from_redis(polygon_id)` so a bad cache is not reused.
- The user sees: "Migration failed and all changes have been rolled back. Reason: ...".

## 8. Running the tests

```
python manage.py test problems
```

`problems/tests.py` covers the S3 file layout and zero-padding, prefix-safe deletion, provider selection from settings, and the migration flow (skips incomplete test cases, clears old files first, `delete_problem_files` never raises).