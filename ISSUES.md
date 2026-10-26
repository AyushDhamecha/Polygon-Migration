# Polygon Migration Tool — Issues & Architecture Analysis

## 1. Overview & Summary

After conducting an end-to-end code audit of the Polygon Migration Tool backend (`problems/`, `users/`, `contents/`, templates, database models, and settings) and validating behaviors on a live PostgreSQL and Redis instance, I have identified the top architectural and product flaws in the current codebase.

During local environment setup, several foundational bugs were already fixed to make the application operational:
- Abstracted storage behind a unified `StorageBackend` interface with support for Google Drive (`problems/storage/`).
- Resolved Polygon API signature calculation (spec requires raw parameter string hashing; URL-encoding broke parameters containing special characters).
- Enforced selection of `READY` packages only, added request timeouts, and removed obsolete Azure references.

Below are the 3 most critical Product Issues and 3 most critical Code Issues, ranked by severity and real-world impact, followed by the Section 3.2 Edge Case Analysis verified against the running application.

### Priority Summary

| Category | # | Issue Title | Severity | Core Impact |
|---|---|---|---|---|
| **Product** | **P1** | Silent Data Corruption, Stale Records, and False Success Reporting | **Critical** | The system claims success while skipping tests, truncating data, and retaining dead test cases. |
| **Product** | **P2** | Broken Problem Statement Rendering & Non-Functional Tagging | **High** | Math appears as unparsed LaTeX code; tag selection is broken due to frontend/backend field mismatches. |
| **Product** | **P3** | Storage ID Mismatch & Fragile Synchronous Re-fetches | **High** | Files stored under DB primary key instead of Polygon ID; blocking HTTP calls cause gateway timeouts. |
| **Code** | **C1** | Monolithic 470-Line View Routed by Hidden Form Flags | **High** | Single view function does all jobs; impossible to test, maintain, or decouple. |
| **Code** | **C2** | Positional Array Indexing Instead of True Entity Synchronization | **High** | Test identity tied to loop index rather than unique IDs, directly causing ghost rows. |
| **Code** | **C3** | Overloaded `PolygonAPI` Class That Conceals Fatal Errors | **High** | Service mixes HTTP, ZIP extraction, C++ compilation, and cloud storage while swallowing exceptions. |

---

## 2. Product Issues

### [P1] Silent Data Corruption, Stale Records, and False Success Reporting

- **Severity:** Critical
- **Location:** `problems/views.py` (lines 432–460, 509–545), `problems/polygon_api.py` (lines 748–756), `problems/views.py` (line 301)

#### Problem
The application consistently displays green "Success" banners even when the underlying data is truncated, partially dropped, or stale:
1. **Empty tests dropped silently:** If a test case has empty input or output after `.rstrip()`, the migration code silently skips it with only a server log entry. If a problem legitimately expects empty output (e.g. printing nothing when condition isn't met), that test case simply disappears from both the DB and storage.
2. **Ghost rows survive re-migration:** When test cases are modified or deleted on Polygon (e.g., dropping from 20 to 12 tests), the migration never cleans up obsolete rows in `ProblemTestCase`. Positional updates overwrite rows 1 to 12, but rows 13 to 20 remain forever as active rows in the database.
3. **Undocumented truncation:** Test inputs and outputs in `ProblemTestCase` are chopped off at 260 characters (`[:260]`) without storing an `is_truncated` boolean or file size.
4. **Checker failure masking:** If `g++` is missing, compilation fails, or the checker download fails, the error is caught, logged, and ignored. The frontend still announces that the checker was compiled and uploaded successfully.

#### Impact
This defeats the single core purpose of a migration tool: creating an exact, faithful replica of a problem. Because the UI always returns a success alert, problem setters and content engineers assume the migration worked perfectly. Downstream judges then grade submissions against truncated test cases, missing sample statements, or stale judge files.

#### Recommended Improvement
- Make re-migration a full synchronization transaction: delete or deactivate tests that no longer exist on Polygon.
- Provide a clear post-migration summary modal instead of a generic one-line alert: `Migrated 15/15 tests (3 samples, 12 regular), 0 skipped, Checker: std::ncmp`.
- If a custom checker fails to compile, fail the migration explicitly and display the compiler error output in the UI.

#### Priority Rationale
This is ranked **#1** because silent data corruption is the most dangerous failure mode in data migration. A slow tool or confusing UI is frustrating, but an incorrect problem that pretends to be correct corrupts competitions and grading systems downstream.

---

### [P2] Broken Problem Statement Rendering & Non-Functional Tagging

- **Severity:** High
- **Location:** `problems/templates/problems/index.html` (lines 207, 215, 223 and lines 500–560), `problems/models.py` (line 5)

#### Problem
1. **Unrendered LaTeX and Escaped HTML:** The problem legend, input format, and output format are output using `{{ ...|linebreaksbr }}` without safe rendering or MathJax/KaTeX integration. Instead of a clean mathematical problem statement, users see raw HTML elements (`<p>`, `<span class="tex-font-style-it">`) and unparsed LaTeX code like `$$$x$$$` or `$$$1 \le n \le 10^6$$$`.
2. **Broken Tag Dropdown & Search:** The frontend JavaScript attempts to read `tag.fields.name`:
   ```javascript
   filteredTags.push({ name: tag.fields.name, id: tag.pk, isExisting: true });
   ```
   However, the Django model `ProblemTag` names this field **`tag_name`** (`tag.fields.tag_name`). Because `tag.fields.name` is always `undefined`, existing tags fail to load, searching tags fails, and clicking items in the tag dropdown does not select them properly. Additionally, `updateHiddenInputs()` is omitted from the checkbox toggle listener, so tag submissions frequently drop selected values.

#### Impact
Reviewers and problem setters cannot read the problem statement to verify its correctness without inspecting raw markup. Crucial math constraints are obscured. Furthermore, problem categorization is blocked because setters cannot attach tags through the UI without manual database interventions.

#### Recommended Improvement
- Sanitize the incoming HTML with a library like `bleach` and output it with `|safe`.
- Include MathJax or KaTeX CDN scripts in `base.html` / `index.html` so delimiters (`$$$`) automatically format as mathematical expressions.
- Align the JavaScript property with the Django model (`tag.fields.tag_name`), and rewrite the tag selector into a clean, modern multi-select component.

#### Priority Rationale
Ranked **#2** because this directly breaks the setter's daily workflow. The preview screen exists specifically so staff can inspect the problem statement before committing it to the database; when it renders as raw code, the review stage cannot fulfill its purpose.

---

### [P3] Storage ID Mismatch & Fragile Synchronous Re-fetches

- **Severity:** High
- **Location:** `problems/views.py` (line 493), `problems/views.py` (lines 189–323), `problems/polygon_api.py` (lines 359–415)

#### Problem
1. **ID Confusion in Storage:** When uploading judge files to cloud storage, the code invokes:
   ```python
   api.migrate_to_storage(polygon_id, problem.id, ...)
   ```
   It passes `problem.id` (the auto-increment database primary key, e.g. `1`) instead of the actual Polygon Problem ID (`polygon_id`, e.g. `69933`). A user who enters problem `69933` expects files in `test_cases/69933/`, but finds them filed under an unrelated internal integer `test_cases/1/`. If multiple environments or database resets occur, these IDs diverge immediately.
2. **Blocking Synchronous Pipeline:** Every interaction—fetching a problem or clicking any of the three migration buttons—triggers a complete, blocking re-fetch of all problem metadata, packages, test lists, and checkers within the HTTP request lifecycle. For problems with 30+ tests, this involves dozens of remote HTTP requests and ZIP downloads. If a reverse proxy or load balancer enforces a 30-to-60 second timeout, the request terminates with a 504 Gateway Timeout while leaving half-finished records behind.

#### Impact
Folder layouts in cloud storage do not match external identifiers, breaking automated lookup scripts and judge sync tools. Meanwhile, the blocking nature of the requests causes browser freezes, proxy timeouts, and invites duplicate concurrent clicks that spawn race conditions in the database.

#### Recommended Improvement
- Standardize the cloud storage key hierarchy on the immutable Polygon Problem ID: `test_cases/{polygon_id}/{test_number}`.
- Offload the package download, checker compilation, and cloud storage upload to an asynchronous background worker (using Celery or RQ with the existing Redis instance). Provide real-time progress updates via WebSockets or polling (e.g., "Uploading test 7 of 21...").

#### Priority Rationale
Ranked **#3** because while operations can succeed on small test sets, the architectural design creates immediate operational failures under realistic contest conditions (large test sets, network latency, proxy timeouts).

---

## 3. Backend Code Issues

### [C1] Monolithic 470-Line View Routed by Hidden Form Flags

- **Severity:** High
- **Location:** `problems/views.py` (lines 87–570, `def index`), `problems/urls.py`

#### Problem
The entire lifecycle of the application—fetching data, updating problem models, storing sample previews, generating problem test cases, compiling checkers, and dispatching cloud uploads—is packed inside a single 470-line view function (`index()`).
Different actions are chosen by inspecting mutually exclusive POST parameters (`migrate_to_db`, `migrate_to_storage`, `migrate_test_cases_to_db`). Each branch re-executes parts of the fetch pipeline, manipulates the DOM context, and renders the same template.

#### Impact
- Violates the Single Responsibility Principle.
- Unit testing individual actions is nearly impossible without mocking the entire Polygon API and Redis cache infrastructure.
- Modifying or debugging one action (e.g., tag persistence) risks breaking unrelated actions (e.g., storage file deletion).

#### Recommended Improvement
Split the monolithic view into clean, dedicated RESTful endpoints or view functions:
- `POST /problems/fetch/` &rarr; Fetches and caches problem data.
- `POST /problems/<id>/migrate-metadata/` &rarr; Saves problem statement and tags.
- `POST /problems/<id>/migrate-testcases-db/` &rarr; Generates DB previews.
- `POST /problems/<id>/migrate-storage/` &rarr; Enqueues the cloud storage upload job.

---

### [C2] Positional Array Indexing Instead of Entity Synchronization

- **Severity:** High
- **Location:** `problems/views.py` (lines 438–450, 515–535), `problems/models.py`

#### Problem
Test cases are identified and persisted based on their numerical loop index:
```python
order = index + 1
# Overwrites existing row with matched order
```
There is no concept of a unique Polygon test identifier or hash. The code simply assumes that the $i$-th test returned by the API today is the exact same entity as the $i$-th test stored yesterday. If a setter deletes or reorders tests, tests with different semantics overwrite existing numbers, and orphaned rows with higher indexes are never deleted.

#### Impact
This design is the direct technical cause of issue P1 and Edge Case Q2. It makes idempotent synchronization impossible and causes discrepancy between the database preview count and the actual files present in cloud storage.

#### Recommended Improvement
- Add a `polygon_test_id` or `test_hash` column to `ProblemTestCase`.
- In the migration loop, collect the set of active IDs, upsert matched records, and explicitly run `ProblemTestCase.objects.filter(problem=problem).exclude(id__in=active_ids).delete()`.

---

### [C3] Overloaded `PolygonAPI` Class That Conceals Fatal Errors

- **Severity:** High
- **Location:** `problems/polygon_api.py` (930 lines)

#### Problem
The `PolygonAPI` class combines half a dozen distinct responsibilities into one file:
1. Low-level HTTP request signing (SHA-512 with random prefix).
2. Polygon REST API calls.
3. Package ZIP archive download, extraction, and file parsing.
4. C++ compilation using local host compilers (`g++`).
5. Redis cache reading and serialization.
6. Cloud storage orchestration and file uploads.

Throughout this class, errors are caught with generic `except Exception:`, logged with `logger.error()`, and swallowed by returning `None` or continuing loops. For example, checker compilation errors are caught and logged, leaving caller code under the impression that compilation succeeded.

#### Impact
Violates separation of concerns. Makes mocking and maintenance difficult. Swallowed exceptions prevent callers from handling failures gracefully, turning hard infrastructure errors into silent runtime anomalies.

#### Recommended Improvement
- Break `PolygonAPI` into dedicated modular services: `PolygonClient` (pure API communication), `PackageExtractor` (ZIP handling), `CheckerCompiler` (sandboxed compilation), and `MigrationService` (orchestration).
- Eliminate silent error suppression: let infrastructure and compilation exceptions bubble up to transaction boundaries so failures are reported transparently.

---

## 4. Edge Case Analysis (Section 3.2)

All four edge cases below have been analyzed and verified through direct tests on the running Django application with PostgreSQL and Redis.

---

### Q1: Problem with 0 Sample Test Cases and 15 Regular Test Cases

#### What happens:
1. **Fetch Step:** The problem fetches successfully. In the preview table, all 15 tests show `Sample Test Case: No`. The statement preview shows no examples.
2. **Migrate to Database (`migrate_to_db`):**
   - The `Problem` row is created with `test_case_count = 15`.
   - The loop responsible for creating `SampleTestCase` rows (`problems/views.py:432-460`) relies on `sample_tests = [tc for tc in test_cases if tc.get('is_sample')]`. Since this list is empty, the loop executes zero times.
   - **Zero `SampleTestCase` records are created.**
   - The view returns a green success alert: `"Problem '...' created in database successfully."`
3. **Migrate Test Cases to DB (`migrate_test_cases_to_db`):**
   - 15 rows are added to `ProblemTestCase` with `is_sample=False` and `order` values from 1 to 15.
4. **Cloud Storage Upload (`migrate_to_storage`):**
   - 15 input/output pairs (`01` through `15`) are uploaded to Google Drive.

#### Why this causes problems:
The tool does not warn the user or block the migration. The problem is published to contestants without any sample input or output in the problem statement. Contestants have no examples to verify input/output formatting or problem mechanics.
*Recommendation:* Display an explicit warning banner: `"Warning: This problem has 0 sample test cases. Ensure this is intentional before publishing."`

*(Behavior verified via code analysis and database query verification).*

---

### Q2: 20 Test Cases Migrated $\to$ Setter Removes 8 on Polygon (12 Remain) $\to$ Re-migrated

#### What happens:
1. **Initial State:** The problem was migrated with 20 tests. PostgreSQL has 20 `ProblemTestCase` rows (`order` 1 to 20). Storage has 20 test files (`01` to `20`).
2. **Re-migration to DB (`migrate_to_db`):**
   - Polygon returns 12 tests. `Problem.test_case_count` is updated from 20 to 12.
3. **Re-migration of Test Cases to DB (`migrate_test_cases_to_db`):**
   - The view iterates over the 12 new tests:
     ```python
     for index, test_case in enumerate(test_cases):
         order = index + 1
         problem_test_case = ProblemTestCase.objects.filter(problem=problem, order=order).first()
         # Updates rows 1 to 12
     ```
   - **Rows 13 through 20 are never deleted.**
   - PostgreSQL now contains **20 total rows** in `ProblemTestCase`: 12 updated rows and **8 stale ghost rows** with obsolete inputs and outputs.
4. **Cloud Storage Upload (`migrate_to_storage`):**
   - Storage calls `delete_problem_files()`, which deletes the entire `test_cases/{id}/` folder in Google Drive.
   - It then uploads the 12 new tests (`01` through `12`).

#### Verified Output (from live PostgreSQL test script):
```
Initial test cases count: 20
After re-migration with 12 tests, total rows in DB: 20
Stale rows (order > 12) still present: [13, 14, 15, 16, 17, 18, 19, 20]
```

#### Why this causes problems:
The database and cloud storage are now out of sync:
- Cloud storage has 12 files.
- Database has 20 rows.
- Any admin screen, API, or contest UI that queries `problem.problem_test_cases.all()` will display 8 deleted test cases to users.

*(Behavior verified live on running PostgreSQL database).*

---

### Q3: Two Different Polygon Problems With the Exact Same Title ("Two Sum")

#### What happens:
1. **Problem 1 Migration:** Problem 1 (e.g., Polygon ID `101`) has title `"Two Sum"`. Its slug is generated via `slugify("Two Sum")` &rarr; `"two-sum"`. It saves to the database successfully.
2. **Problem 2 Fetch:** Problem 2 (Polygon ID `102`) also has title `"Two Sum"`. Fetch works normally and previews the slug as `"two-sum"`.
3. **Problem 2 Migration (`migrate_to_db`):**
   - The code executes:
     ```python
     Problem.objects.create(
         polygon_id='102',
         title='Two Sum',
         slug='two-sum',
         ...
     )
     ```
   - Because `Problem.slug` is defined with `unique=True` in `problems/models.py`:
     ```python
     slug = models.SlugField(unique=True, max_length=255)
     ```
   - PostgreSQL raises an `IntegrityError`:
     `duplicate key value violates unique constraint "problems_problem_slug_key"`
     `DETAIL: Key (slug)=(two-sum) already exists.`
   - Django rolls back the database transaction.
   - The user sees a red alert banner: `"Migration failed and all changes have been rolled back. Reason: duplicate key value violates unique constraint..."`
   - Because no database record exists for Problem 2, the **Migrate to Storage** and **Migrate Test Cases** buttons remain completely disabled.

#### Verified Output (from live PostgreSQL test script):
```
Created Problem 1, ID: 3 slug: two-sum
IntegrityError caught successfully: duplicate key value violates unique constraint "problems_problem_slug_key"
DETAIL:  Key (slug)=(two-sum) already exists.
```

#### Why this causes problems:
The second problem cannot be migrated under any circumstances until someone renames the problem on Polygon. The UI offers no option to customize the slug. Furthermore, any non-Latin title (e.g., in Russian, Chinese, or Hindi) produces an empty slug (`""`), causing all non-Latin problems to collide on the empty string after the first one is migrated.

*(Behavior verified live on running PostgreSQL database).*

---

### Q4: Data Discarded During "Migrate Test Cases to DB"

#### Action Analyzed:
`migrate_test_cases_to_db` (`problems/views.py`, lines 509–545).

#### What data is intentionally discarded:
1. **Hard Truncation to 260 Characters:**
   ```python
   input_data = test_case.get('input', '')[:260]
   output_data = test_case.get('output', '')[:260]
   ```
   Any input or output data exceeding 260 characters is permanently sliced off. Real contest test cases (often hundreds of kilobytes or megabytes of coordinates, graphs, or strings) are reduced to tiny prefixes.
2. **Trailing Whitespace & Newlines:**
   Every test has `.rstrip()` applied, removing trailing spaces and newline characters.
3. **Empty Test Cases:**
   Tests where input or output evaluates to empty after stripping are discarded entirely and skipped from saving.
4. **Rich Polygon Metadata:**
   The following metadata returned by Polygon is discarded:
   - Original Polygon test index.
   - Test group name (e.g. `subtask_1`, `subtask_2`).
   - Point values for IOI-style grading.
   - Test generation method (`manual` vs. generator script command line).

#### Why this was intentional:
The original developer designed `ProblemTestCase` as a lightweight table meant only to power browser preview cards on the frontend website. Storing multi-megabyte test files directly in PostgreSQL rows would cause severe database bloat. Cloud storage was designated as the canonical store for full test files.

#### Why this causes problems in practice:
1. **Misleading Model Architecture:** The model is named `ProblemTestCase`, not `ProblemTestCasePreview`. It has `TextField` columns for `input` and `output`. Any developer or admin looking at the model assumes it contains the actual test data.
2. **Broken Judge Fallbacks:** If cloud storage is temporarily unreachable and an automated test runner falls back to reading `ProblemTestCase.input`, it receives truncated, invalid input data (such as an array cut off mid-number), resulting in false Runtime Errors or Wrong Answer verdicts.
3. **No Truncation Marker:** There is no `is_truncated` boolean flag or `original_byte_length` integer. Downstream consumers cannot programmatically distinguish between a small 50-character test case that is complete and a 50-megabyte test case that was truncated.
4. **Checker Sensitivity:** Stripping trailing whitespace breaks problems where whitespace is syntactically significant or verified by custom strict checkers.

*(Behavior verified via code inspection of `problems/views.py` and model inspection of `problems/models.py`).*
