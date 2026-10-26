# Polygon Migration Tool — How It Works

This doc walks through every user-facing action in the order you'd take them: log in, fetch a problem, review it, migrate it to the DB, migrate test cases to the DB, and finally upload test files to cloud storage. It also explains how the Polygon API integration and cloud storage naming work.

All file paths below are relative to `PolygonMigration/`.

---

## Quick architecture map

```
Browser
  └── POST / ──────────────────────────────────────> problems/views.py  (index)
                                                        ├── PolygonAPI  ──────> polygon.codeforces.com
                                                        ├── Django ORM  ──────> PostgreSQL
                                                        ├── Redis (cache)
                                                        └── StorageBackend ──> Google Drive / Azure Blob
```

Every button on the main page submits a `POST /` to the same `index()` view. A hidden form field tells the view which action to run. The view always re-fetches the problem from Polygon at the same time, so the page re-renders with fresh data.

---

## 1. Logging in

**URL:** `/users/login/`

If you open `/` without being logged in, Django redirects you to the login page automatically.

**What you enter:** your email and password.

**What happens:**
1. Django tries both `EmailBackend` (custom) and the default `ModelBackend`.
2. `EmailBackend` looks up the user by email and checks the password with `check_password()`.
3. If the user is found, active, and `is_staff = True`, you are logged in and redirected to the main page.
4. If the credentials are wrong, or the account is not staff, you see a red error message and stay on the login page.

**Session:** stays alive for 24 hours (controlled by `SESSION_COOKIE_AGE = 86400`).

**Creating a superuser:** run `python manage.py createsuperuser` in the `PolygonMigration/` directory. It will ask for name, email, password, and a few extra fields. Superusers have `is_staff = True` automatically.

---

## 2. Fetching a problem from Polygon

**What you do:** type a numeric Polygon problem ID (e.g. `615237`) into the input box and click **Fetch Problem**. A loading overlay appears — this can take a few minutes because the app downloads the full problem package and every test case from Polygon one by one.

**What the app does behind the scenes:**

1. Creates a `PolygonAPI` instance using `POLYGON_API_KEY` and `POLYGON_API_SECRET` from the environment.
2. Checks whether this problem is already in the database. If it is, it pre-fills the difficulty and tags, and fetches the reference solution so you can see it straight away.
3. Calls `problem.info` to get the time limit, memory limit, and other metadata.
4. Downloads the problem package (a ZIP file from Polygon) and extracts `problem.html`. This HTML file is parsed with lxml to pull out the problem title, statement (legend), input/output format, and any notes.
5. Fetches all test cases. For each test it calls `problem.testInput` and `problem.testAnswer` — that is two API calls per test, which is why the fetch is slow for problems with many tests.
6. Caches the test cases in Redis for 30 minutes so you do not have to re-download them if you click a migration button right after.
7. Builds 50-character previews for the test case table.
8. Renders the page with everything.

**What you see after fetching:**
- Problem title, Polygon ID, time and memory limits, checker type
- Problem statement (with LaTeX math rendered), input/output format, notes
- A table of all test cases with previews and a "View Full" button
- The reference solution (if the problem is already in the database)

**Polygon prerequisites:** before fetching, make sure:
- Your API key has write access to the problem
- A "READY" package exists — build one from the problem Packages page after committing
- Sample test cases are marked "Use in statements" on Polygon

---

## 3. Migrating the problem to the database

**What you do:** after fetching, select a **Difficulty** (required), add at least two tags using the search box, and click **Create/Update problem in Database**.

You can type in the tag box to filter existing tags and click one to add it. Pressing Enter also adds the typed tag. Tags are shown as removable chips. If you have not selected a difficulty, the form will not submit — there is a client-side check for this.

**What the app does:**

1. Runs the same fetch pipeline as above (using the Redis cache for test cases).
2. Validates that difficulty is set. If not, it raises an error and rolls back.
3. Looks up the problem by Polygon ID in the database:
   - If it exists, it updates the existing row (title, statement, limits, difficulty, notes, etc.)
   - If it does not exist, it creates a new row
4. Clears the current tags for this problem and re-adds all submitted tags. New tag names are created in the `ProblemTag` table automatically.
5. Saves sample test cases (those marked "Use in statements" on Polygon) to `SampleTestCase`. These are what gets shown alongside the problem statement on the platform.
6. Everything happens inside a DB transaction — if anything goes wrong, nothing is saved.

**What you see after:**
- A blue success alert: "Problem 'X' created/updated in database successfully."
- The internal DB ID and a link
- The reference solution is now shown
- The "Migrate Test Cases to Cloud Storage" and "Migrate Test Description to Database" buttons become active

---

## 4. Migrating test cases to the database

**What you do:** click **Migrate Test Description to Database**.

This stores a short **preview** of every test case in the database — not the full judge data. The purpose is so the platform can display test info (sample flag, description, truncated input/output) without hitting cloud storage.

**What the app does:**

1. Reads test cases from Redis (or re-fetches from Polygon on a cache miss).
2. For each test case:
   - Strips trailing whitespace from input and output
   - Skips the test if either side is empty
   - Truncates both to **260 characters**
   - If a `ProblemTestCase` row already exists at this position, it is updated; otherwise a new one is created

**What you see after:** a green success alert confirming the migration.

---

## 5. Uploading test cases to cloud storage

**What you do:** click **Migrate Test Cases to Cloud Storage**.

This uploads the **full** input and output files to Google Drive (or Azure Blob, depending on configuration). These are the actual judge files.

**What the app does:**

1. Confirms the problem exists in the database first. If not, it shows an error.
2. Deletes any previously uploaded files for this problem from storage (clean slate).
3. Reads test cases from Redis (or re-fetches from Polygon on a cache miss).
4. Uploads each test case as two files:
   - `test_cases/{polygon_id}/{NN}` — the input
   - `test_cases/{polygon_id}/{NN}.a` — the expected output

   `{polygon_id}` is the Polygon problem ID (e.g. `615237`), and `{NN}` is the test number zero-padded to two digits (e.g. `01`, `02`).

5. If the problem has a custom checker (anything not in Codeforces standard checker list), the app:
   - Downloads the checker source from Polygon
   - Tries to compile it with `g++ -std=gnu++17 -O2`
   - Uploads the compiled binary as `test_cases/{polygon_id}/custom_checker` (or `custom_checker.exe` on Windows), or the `.cpp` source if compilation fails

**Rollback:** if anything goes wrong, the DB transaction rolls back and the app tries to delete any partially uploaded storage files so they do not get out of sync.

**What you see after:** a green alert, "Test cases migrated to cloud storage successfully."

---

## 6. External integrations

### 6.1 Polygon API

Every Polygon API call is a signed `POST` to `https://polygon.codeforces.com/api/{method}`.

**How signing works** (`PolygonAPI._generate_api_sig`):
1. Take all request parameters plus `apiKey` and the current Unix timestamp.
2. Sort them alphabetically by key name.
3. Join them as `key=value` pairs separated by `&` (raw values, not URL-encoded).
4. Generate 6 random characters called `rand`.
5. Hash `{rand}/{method}?{params}#{POLYGON_API_SECRET}` with SHA-512.
6. Append the hash to `rand` to get `apiSig`.

Polygon returns JSON shaped as `{"status": "OK", "result": ...}` or `{"status": "FAILED", "comment": "..."}`. The app raises an exception on `FAILED` or any 4xx response.

Some endpoints (test inputs, answers, solution content) return plain text instead of JSON.

**API methods used:**

| Method | What it is used for |
|---|---|
| `problem.info` | Time limit, memory limit |
| `problem.packages` | Find the newest READY package |
| `problem.package` | Download the ZIP with problem.html |
| `problem.tests` | List all tests with flags and descriptions |
| `problem.testInput` / `problem.testAnswer` | Full input and output for each test |
| `problem.checker` | Checker name (e.g. `std::ncmp.cpp`) |
| `problem.viewFile` | Download custom checker source |
| `problem.solutions` / `problem.viewSolution` | Fetch the main reference solution |
| `problem.updateWorkingCopy` | Refresh Polygon working copy (for already-migrated problems) |

### 6.2 Cloud storage naming and organisation

The storage interface is defined in `problems/storage/base.py`. Both Google Drive and Azure Blob implement the same interface, so swapping providers does not touch any migration logic.

**File layout:**
```
test_cases/{polygon_id}/{NN}                    input file for test NN
test_cases/{polygon_id}/{NN}.a                  expected output for test NN
test_cases/{polygon_id}/custom_checker[.exe]    compiled checker (or .cpp if compilation failed)
```

- `{polygon_id}` is the **Polygon problem ID** (e.g. `615237`), not the internal database ID. This keeps the storage layout independent of which database the tool is connected to and makes it easy to find files by the ID you know from Polygon.
- `{NN}` is the test position (1-based), zero-padded to two digits.
- All content is stored as UTF-8 bytes with MIME type `application/octet-stream`.

**Google Drive** (`STORAGE_PROVIDER=gdrive`):
- The app maps path segments to real Drive folders. Given a root folder ID (`GOOGLE_DRIVE_FOLDER_ID`), it creates or reuses a `test_cases` folder, then a sub-folder named after the Polygon ID, then uploads files inside it.
- Folder lookups are cached in memory per request to avoid redundant `files.list` calls.
- Files are created with `files.create` or updated with `files.update` if they already exist. Files over 5 MB use resumable upload.
- Deleting a folder permanently removes all files inside it (no trash).
- Credentials: either an OAuth Desktop app client JSON + a token from `python manage.py gdrive_auth`, or a service-account JSON (service accounts only work with Shared Drives).
- Required scope: `https://www.googleapis.com/auth/drive`.

**Azure Blob** (`STORAGE_PROVIDER=azure`):
- Uses the same path strings as blob names inside `AZURE_CONTAINER_NAME`.
- Auth: `UsernamePasswordCredential` (MFA is not supported).

### 6.3 Redis caching

Redis is used to avoid re-downloading all test cases from Polygon on every button click.

| Key pattern | Written by | TTL | Purpose |
|---|---|---|---|
| `polygon_migration_test_cases_{polygon_id}_count` | `store_test_cases_in_redis` | 30 min | Number of cached tests |
| `polygon_migration_test_cases_{polygon_id}_test_{n}` | `store_test_cases_in_redis` | 30 min | JSON for each individual test |
| `oj_dev_with_redis_storage_test_cases_{polygon_id}*` | online judge (external) | — | Deleted before a storage upload so the judge picks up new files |

Redis errors are caught and treated as a cache miss — the app falls back to Polygon automatically.

---

## 7. Environment variables

| Variable | What it controls |
|---|---|
| `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` | Standard Django settings |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | PostgreSQL connection |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_SSL` | Redis connection |
| `POLYGON_API_KEY`, `POLYGON_API_SECRET` | Polygon API credentials |
| `STORAGE_PROVIDER` | `gdrive` or `azure` |
| `GOOGLE_DRIVE_CREDENTIALS_FILE` | Path to OAuth client or service-account JSON |
| `GOOGLE_DRIVE_TOKEN_FILE` | Path to the OAuth token file (from `gdrive_auth`) |
| `GOOGLE_DRIVE_FOLDER_ID` | Root Google Drive folder ID |
| `AZURE_STORAGE_ACCOUNT_URL`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_USERNAME`, `AZURE_PASSWORD`, `AZURE_CONTAINER_NAME` | Azure Blob settings |
| `CUSTOM_CHECKER_DIR` | Optional working directory for compiling custom checkers |

Logs go to the console and to `logs/project.log` (log level DEBUG for the `problems` logger).
